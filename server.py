"""
server.py  –  self-contained FastAPI backend
Run from the SAME folder this file is in:
    uvicorn server:app --reload --port 8000
"""
from __future__ import annotations
import os, sys, time, uuid, threading

# ── make sure sibling modules are importable regardless of cwd ──────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from environment import DatacenterEnvironment
from guardrails   import Guardrails
from feedback     import FeedbackLoop
from agent        import DatacenterAgent, TEMP_THRESHOLD_C, MIN_NET_PROFIT
from explainable  import ExplainableAI

# ── config ───────────────────────────────────────────────────────────────────
ADMIN_USERNAME        = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD        = os.getenv("ADMIN_PASSWORD", "changeme")
SESSION_TTL           = 60 * 60 * 4          # 4 hours
NUM_REGIONS           = int(os.getenv("NUM_REGIONS", 5))
SERVERS_PER_REGION    = int(os.getenv("SERVERS_PER_REGION", 3))
CHIPSETS_PER_SERVER   = int(os.getenv("CHIPSETS_PER_SERVER", 4))
BUFFER_WINDOW         = int(os.getenv("BUFFER_WINDOW_SECONDS", 5))
DECISION_INTERVAL     = int(os.getenv("DECISION_INTERVAL_SECONDS", 5))
FEEDBACK_WINDOW       = int(os.getenv("FEEDBACK_WINDOW_SECONDS", 30))

# ── boot the simulation ───────────────────────────────────────────────────────
print("Booting datacenter simulation …")
env         = DatacenterEnvironment(NUM_REGIONS, SERVERS_PER_REGION,
                                     CHIPSETS_PER_SERVER, BUFFER_WINDOW)
guardrails  = Guardrails()
feedback    = FeedbackLoop(env, FEEDBACK_WINDOW)
agent       = DatacenterAgent(env, guardrails, feedback, DECISION_INTERVAL)
explainer   = ExplainableAI(env, agent, guardrails)

def _tick_loop():
    while True:
        env.tick(1.0)
        time.sleep(1.0)

threading.Thread(target=_tick_loop, daemon=True).start()
agent.run_forever()
print("Simulation running ✓")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Datacenter AI Agent")

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()

# ── lightweight abuse guard for /api/ask ──────────────────────────────────
# /api/ask is intentionally public (it's the "Ask the Agent" box on the
# dashboard) but it makes a real LLM call, so an unlimited endpoint is a
# cost-abuse vector. This is a simple fixed-window limiter, good enough for
# a single-process demo; swap for a proper limiter (e.g. slowapi/Redis) if
# this is ever deployed somewhere with real traffic.
ASK_RATE_LIMIT = int(os.getenv("ASK_RATE_LIMIT_PER_MINUTE", 6))
_ask_hits: dict[str, list[float]] = {}
_ask_lock = threading.Lock()


def _check_ask_rate_limit(client_ip: str):
    now = time.time()
    with _ask_lock:
        hits = [t for t in _ask_hits.get(client_ip, []) if now - t < 60]
        if len(hits) >= ASK_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Too many questions — limit is {ASK_RATE_LIMIT} per minute.",
            )
        hits.append(now)
        _ask_hits[client_ip] = hits

# ── auth ──────────────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    username: str
    password: str

class RuleBody(BaseModel):
    rule: str

class ResolveBody(BaseModel):
    decision: str

class AskBody(BaseModel):
    question: str

def _require_admin(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin token")
    token = authorization.removeprefix("Bearer ").strip()
    with _sessions_lock:
        expiry = _sessions.get(token)
        if not expiry or expiry < time.time():
            raise HTTPException(status_code=401, detail="Invalid or expired session")

# ── public endpoints ──────────────────────────────────────────────────────────
@app.get("/api/state")
def get_state():
    snap = (list(env.live_buffer) or list(env.frozen_buffer) or [env._snapshot()])[-1]
    return {
        "snapshot": snap,
        "decision_interval_seconds": agent.decision_interval,
        "next_decision_in": agent.decision_interval - (int(time.time()) % agent.decision_interval),
        "open_warning_count": len(guardrails.open_warnings()),
        "temp_threshold_c": TEMP_THRESHOLD_C,
    }

@app.get("/api/log")
def get_log():
    return {"entries": list(reversed(agent.decision_log[-25:]))}

@app.get("/api/feedback")
def get_feedback():
    return {"entries": feedback.recent_summary(25)}

@app.post("/api/ask")
def ask(body: AskBody, request: Request):
    _check_ask_rate_limit(request.client.host if request.client else "unknown")
    return {"answer": explainer.ask(body.question.strip())}

# ── admin endpoints ───────────────────────────────────────────────────────────
@app.post("/api/admin/login")
def admin_login(body: LoginBody):
    if body.username != ADMIN_USERNAME or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[token] = time.time() + SESSION_TTL
    return {"token": token, "expires_in": SESSION_TTL}

@app.get("/api/admin/warnings")
def admin_warnings(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    return {"warnings": [w.__dict__ for w in guardrails.open_warnings()]}

_EXECUTABLE_ACTIONS = {
    "shift_within_server": lambda payload: env.shift_within_server(**payload),
    "shift_within_region": lambda payload: env.shift_within_region(**payload),
    "migrate_region": lambda payload: env.migrate_region(**{**payload, "min_net_profit": MIN_NET_PROFIT}),
}


@app.post("/api/admin/warnings/{warning_id}/resolve")
def admin_resolve(warning_id: str, body: ResolveBody,
                   authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    warning = guardrails.resolve_warning(warning_id, body.decision)
    if warning is None:
        raise HTTPException(status_code=404, detail="Warning not found")

    executed = False
    outcome = None
    if body.decision == "approved" and warning.action in _EXECUTABLE_ACTIONS:
        outcome = _EXECUTABLE_ACTIONS[warning.action](dict(warning.payload))
        agent.log_admin_override(warning.action, warning.payload, warning.reason, outcome)
        executed = bool(outcome.get("ok"))

    return {"ok": True, "executed": executed, "outcome": outcome}

@app.get("/api/admin/donts")
def admin_get_donts(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    return {"rules": guardrails.admin_donts}

@app.post("/api/admin/donts")
def admin_add_dont(body: RuleBody,
                    authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    guardrails.add_dont(body.rule.strip())
    return {"rules": guardrails.admin_donts}

@app.delete("/api/admin/donts")
def admin_remove_dont(body: RuleBody,
                       authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    guardrails.remove_dont(body.rule)
    return {"rules": guardrails.admin_donts}

# ── static files ──────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

if not os.path.isdir(STATIC_DIR):
    os.makedirs(STATIC_DIR, exist_ok=True)
    # write a placeholder so startup doesn't crash even if static/ is empty
    with open(os.path.join(STATIC_DIR, "index.html"), "w") as f:
        f.write("<h1>Put your static/ files here</h1>")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))
