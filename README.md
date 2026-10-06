# Grid Control — Autonomous Datacenter Orchestration Agent

An AI-driven simulation of a multi-region datacenter. A background agent (Claude, GPT, or Gemini) makes shift/migrate decisions every few seconds based on live telemetry, guarded by hard "don't" rules an admin can edit, with an explainability endpoint and an admin console for reviewing high-criticality actions.

## 1. Prerequisites

- Python 3.10+
- pip
- An API key for at least one LLM provider: [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), or [Google AI Studio](https://aistudio.google.com/) (Gemini)

## 2. Clone the repo

```bash
git clone https://github.com/ManasTarare/Data_agent.git
cd Data_agent
```

## 3. Put the frontend files where the server expects them

`server.py` serves everything from a `static/` folder next to it, but the frontend files (`index.html`, `admin.html`, `app.js`, `admin.js`, `style.css`) live at the repo root. Move them in before first run, or the server will boot with a placeholder page instead of your UI:

```bash
mkdir static
mv index.html admin.html app.js admin.js style.css static/
```

## 4. Create a virtual environment

```bash
python -m venv env
```

Activate it:

```bash
# Windows
env\Scripts\activate

# macOS / Linux
source env/bin/activate
```

## 5. Install dependencies

```bash
pip install -r requirements.txt
```

This installs `fastapi`, `uvicorn`, `python-dotenv`, `anthropic`, `openai`, `google-genai`, and `pydantic`.

## 6. Configure environment variables

Create a `.env` file in the project root (same folder as `server.py`):

```ini
# --- LLM provider (pick one) ---
LLM_PROVIDER=anthropic          # anthropic | openai | gemini
LLM_MODEL=claude-sonnet-5

ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...

# --- Admin console login ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=changeme

# --- Simulation shape (all optional, shown with defaults) ---
NUM_REGIONS=5
SERVERS_PER_REGION=3
CHIPSETS_PER_SERVER=4
BUFFER_WINDOW_SECONDS=5
DECISION_INTERVAL_SECONDS=5
FEEDBACK_WINDOW_SECONDS=30

# --- Agent thresholds (optional) ---
TEMP_THRESHOLD_C=68
UTIL_IMBALANCE_THRESHOLD=0.35
MIN_NET_PROFIT=0.0
```

You only need the API key for whichever `LLM_PROVIDER` you choose. Leave `ADMIN_PASSWORD` at the default only for local testing — change it before deploying anywhere shared.

## 7. Run it

**Option A — the launcher (Windows):**

```bash
run.bat
```

This creates/activates the venv, installs dependencies, and starts uvicorn for you.

**Option B — manually (any OS):**

```bash
uvicorn server:app --reload --port 8000
```

## 8. Open it

- Dashboard: **http://localhost:8000**
- Admin console: **http://localhost:8000/admin** (sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`)

## Known issue: Gemini provider crashes

If you set `LLM_PROVIDER=gemini`, decisions will currently fail with `module 'google.genai' has no attribute 'GenerativeAI'`. This is a bug in `agent.py` — the `google-genai` SDK doesn't expose a `GenerativeAI` class. The fix is to use the client-based API instead:

```python
# agent.py, inside LLMClient.complete(), gemini branch — replace:
client = genai.GenerativeAI(api_key=os.getenv("GEMINI_API_KEY"))
resp = client.generate_text(
    model=self.model,
    contents=user_prompt,
    config=types.GenerateTextConfig(
        system_instructions=system_prompt,
        max_output_tokens=500,
        temperature=0.4,
    ),
)
return resp.text

# with:
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
resp = client.models.generate_content(
    model=self.model,
    contents=user_prompt,
    config=types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=500,
        temperature=0.4,
    ),
)
return resp.text
```

Until that's patched, use `LLM_PROVIDER=anthropic` or `LLM_PROVIDER=openai`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser shows "Put your static/ files here" | You skipped step 3 — move the frontend files into `static/`. |
| `ModuleNotFoundError` on startup | Activate the venv before running uvicorn, or re-run `pip install -r requirements.txt`. |
| 401 on admin login | Check `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` match what you're typing. |
| Repeated `ERROR` entries in Agent Decisions | Almost certainly the Gemini bug above — switch providers or apply the patch. |
| Port already in use | Run with a different port: `uvicorn server:app --reload --port 8001`. |
