from __future__ import annotations

import json
import os
import threading
import time

from environment import DatacenterEnvironment
from feedback import FeedbackLoop
from guardrails import Guardrails

TEMP_THRESHOLD_C = float(os.getenv("TEMP_THRESHOLD_C", 68))
UTIL_IMBALANCE_THRESHOLD = float(os.getenv("UTIL_IMBALANCE_THRESHOLD", 0.35))
MIN_NET_PROFIT = float(os.getenv("MIN_NET_PROFIT", 0.0))

DECISION_SCHEMA_HINT = """
Respond with ONLY a valid JSON object.
Do not include Markdown, code fences, comments, or text outside the JSON.

Use exactly this structure:

{
  "reasoning": "Brief explanation of the decision",
  "action": "shift_within_server | shift_within_region | migrate_region | pass",
  "params": {}
}
"""

GEMINI_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string"
        },
        "action": {
            "type": "string",
            "enum": [
                "shift_within_server",
                "shift_within_region",
                "migrate_region",
                "pass",
            ],
        },
        "params": {
            "type": "object"
        },
    },
    "required": [
        "reasoning",
        "action",
        "params",
    ],
}


class LLMClient:
    """LLM provider wrapper for Anthropic, OpenAI, and Gemini."""

    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "gemini").lower()

        default_models = {
            "anthropic": "claude-sonnet-5",
            "openai": "gpt-4o-mini",
            "gemini": "gemini-3.7-flash",
        }

        self.model = os.getenv(
            "LLM_MODEL",
            default_models.get(self.provider, "gemini-3.7-flash"),
        )

    @staticmethod
    def _clean_provider_error(provider_label: str, error: Exception) -> str:
        """Provider SDKs (especially google-genai) can raise exceptions whose
        str() is a multi-hundred-character dump of nested dicts/links. That's
        unreadable when it lands in the decision log, so this reduces it to
        one short, useful line and calls out quota/rate-limit errors
        specifically, since those need a different fix (slow down, switch
        provider, or wait/upgrade) than a genuine bug would."""
        raw = str(error)
        first_line = raw.strip().splitlines()[0] if raw.strip() else raw
        if len(first_line) > 220:
            first_line = first_line[:220] + "…"

        if "RESOURCE_EXHAUSTED" in raw or " 429" in raw or raw.startswith("429"):
            return (
                f"{provider_label} rate limit / quota exceeded (HTTP 429). "
                f"{first_line} — try again later, raise DECISION_INTERVAL_SECONDS, "
                f"or switch LLM_PROVIDER."
            )
        if "401" in raw or "PERMISSION_DENIED" in raw or "invalid api key" in raw.lower():
            return f"{provider_label} rejected the API key (401/permission denied). {first_line}"
        return f"{provider_label} API error: {first_line}"

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(
                api_key=os.getenv("ANTHROPIC_API_KEY")
            )

            try:
                response = client.messages.create(
                    model=self.model,
                    max_tokens=1200,
                    system=system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": user_prompt,
                        }
                    ],
                )
            except Exception as error:
                raise RuntimeError(self._clean_provider_error("Anthropic", error)) from error

            return "".join(
                block.text
                for block in response.content
                if hasattr(block, "text")
            )

        if self.provider == "openai":
            from openai import OpenAI

            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY")
            )

            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                    max_tokens=1200,
                )
            except Exception as error:
                raise RuntimeError(self._clean_provider_error("OpenAI", error)) from error

            return response.choices[0].message.content

        if self.provider == "gemini":
            from google import genai
            from google.genai import types

            api_key = os.getenv("GEMINI_API_KEY")

            if not api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured. "
                    "Add it to your .env file and restart the server."
                )

            client = genai.Client(api_key=api_key)

            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=1200,
                        thinking_config=types.ThinkingConfig(
                            thinking_level="low"
                        ),
                        response_mime_type="application/json",
                        response_schema=GEMINI_DECISION_SCHEMA,
                    ),
                )
            except Exception as error:
                raise RuntimeError(self._clean_provider_error("Gemini", error)) from error

            if not response.text:
                raise RuntimeError(
                    "Gemini returned an empty decision response."
                )

            return response.text

        raise ValueError(
            f"Unsupported LLM_PROVIDER: {self.provider}"
        )


class DatacenterAgent:
    def __init__(
        self,
        env: DatacenterEnvironment,
        guardrails: Guardrails,
        feedback: FeedbackLoop,
        decision_interval: int = 20,
    ):
        self.env = env
        self.guardrails = guardrails
        self.feedback = feedback
        self.decision_interval = decision_interval
        self.llm = LLMClient()

        self.decision_log: list[dict] = []

        self._running = False
        self._stop_event = threading.Event()
        self._log_lock = threading.Lock()

    def _add_log_entry(self, entry: dict):
        with self._log_lock:
            self.decision_log.append(entry)

    def log_admin_override(self, action: str, payload: dict, reason: str, outcome: dict):
        """Records an action an admin manually approved/executed from the
        Warnings queue, so it shows up in the same decision log the
        dashboard already polls."""
        self._add_log_entry({
            "timestamp": time.time(),
            "decision": {
                "reasoning": f"Admin-approved override: {reason}",
                "action": action,
                "params": payload,
            },
            "result": {"status": "executed_by_admin", "outcome": outcome},
        })

    def _system_prompt(self) -> str:
        donts = "\n".join(
            f"- {rule}"
            for rule in self.guardrails.admin_donts
        ) or "(No extra rules have been set by Admin.)"

        return f"""
You are an autonomous datacenter operations agent.

Your only task is to decide whether a single task should be moved between
chipsets, servers, or regions. You must reduce overheating first, improve
latency for priority tasks second, and reduce energy cost/carbon third.

You receive a frozen telemetry snapshot every {self.decision_interval} seconds.

Decision priority:

1. If a chipset is hotter than {TEMP_THRESHOLD_C}C:
   move one eligible task to a cooler chipset on the same server using
   "shift_within_server".

2. If a high-priority task is on a standard server and a powerful server in
   the same region has enough capacity, OR if utilization is badly
   imbalanced across servers in the same region (the busiest server's
   average utilization minus the least-busy server's average utilization
   is greater than {UTIL_IMBALANCE_THRESHOLD}):
   use "shift_within_region" to move one task toward the less-loaded server.

3. If another region is cheaper or greener and:
   projected_savings - bandwidth_cost_estimate > {MIN_NET_PROFIT}
   use "migrate_region".

4. If no safe and useful action is needed:
   use "pass".

Rules:

- You are allowed to move only one task at a time.
- Never shut down a server or region.
- Never move all tasks from a server or region.
- Never move a task that is already locked.
- Never select an action outside the allowed action list.
- If information is incomplete, uncertain, or unsafe, choose "pass".
- "pass" is a correct and expected decision.

Admin rules that you must never violate:

{donts}

Action parameter requirements:

- shift_within_server:
  from_chipset_id, to_chipset_id, task_id

- shift_within_region:
  from_server_id, to_server_id, task_id

- migrate_region:
  from_region_id, to_region_id, task_id,
  bandwidth_cost_estimate, projected_savings

- pass:
  params must be an empty object

{DECISION_SCHEMA_HINT}
"""

    def _user_prompt(
        self,
        buffer_snapshot: list[dict],
        recent_feedback: list[dict],
    ) -> str:
        latest_snapshot = (
            buffer_snapshot[-1]
            if buffer_snapshot
            else {}
        )

        return json.dumps(
            {
                "latest_snapshot": latest_snapshot,
                "recent_action_outcomes": recent_feedback,
            },
            indent=2,
        )

    def _parse_decision(self, raw: str) -> dict:
        try:
            cleaned = raw.strip()

            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                cleaned = cleaned.rsplit("```", 1)[0]

            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")

            if first_brace >= 0 and last_brace >= first_brace:
                cleaned = cleaned[first_brace:last_brace + 1]

            decision = json.loads(cleaned)

            if not isinstance(decision, dict):
                raise ValueError(
                    "LLM response is not a JSON object."
                )

            valid_actions = {
                "shift_within_server",
                "shift_within_region",
                "migrate_region",
                "pass",
            }

            if decision.get("action") not in valid_actions:
                raise ValueError(
                    "LLM response contains an invalid action."
                )

            if not isinstance(
                decision.get("params", {}),
                dict,
            ):
                raise ValueError(
                    "LLM response params must be an object."
                )

            if not isinstance(
                decision.get("reasoning", ""),
                str,
            ):
                raise ValueError(
                    "LLM response reasoning must be text."
                )

            return decision

        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return {
                "reasoning": (
                    f"{self.llm.provider} returned an incomplete or invalid "
                    "JSON decision; safely choosing pass."
                ),
                "action": "pass",
                "params": {},
            }

    def decide_once(
        self,
        buffer_snapshot: list[dict] | None = None,
        recent_feedback: list[dict] | None = None,
    ):
        # This supports manual decisions and the fixed 20-second scheduler.
        if buffer_snapshot is None:
            buffer_snapshot = self.env.swap_buffers()

        if not buffer_snapshot:
            return None

        if recent_feedback is None:
            recent_feedback = self.feedback.recent_summary()

        raw = self.llm.complete(
            self._system_prompt(),
            self._user_prompt(
                buffer_snapshot,
                recent_feedback,
            ),
        )

        decision = self._parse_decision(raw)
        result = self._execute(decision)

        self._add_log_entry(
            {
                "timestamp": time.time(),
                "decision": decision,
                "result": result,
            }
        )

        return result

    def _execute(self, decision: dict) -> dict:
        action = decision.get("action", "pass")
        params = decision.get("params", {}) or {}

        verdict = self.guardrails.evaluate(
            action,
            params,
        )

        if verdict["verdict"] == "pass":
            return {
                "status": "passed"
            }

        if verdict["verdict"] == "block":
            return {
                "status": "blocked_for_admin",
                "warning_id": verdict["warning"].id,
            }

        if action == "shift_within_server":
            outcome = self.env.shift_within_server(
                **params
            )

        elif action == "shift_within_region":
            outcome = self.env.shift_within_region(
                **params
            )

        elif action == "migrate_region":
            # Guard against the LLM's params dict ever including this key
            # itself, then enforce the configured floor for real.
            params = dict(params)
            params.pop("min_net_profit", None)
            outcome = self.env.migrate_region(
                **params,
                min_net_profit=MIN_NET_PROFIT,
            )

        else:
            return {
                "status": "unknown_action"
            }

        if outcome.get("ok"):
            self.feedback.record(
                action,
                params,
            )

        return {
            "status": "executed",
            "outcome": outcome,
        }

    def run_forever(self):
        self._running = True
        self._stop_event.clear()

        def process_snapshot(
            buffer_snapshot: list[dict],
            recent_feedback: list[dict],
        ):
            try:
                self.decide_once(
                    buffer_snapshot=buffer_snapshot,
                    recent_feedback=recent_feedback,
                )

            except Exception as error:
                self._add_log_entry(
                    {
                        "timestamp": time.time(),
                        "error": str(error),
                    }
                )

        def scheduler():
            # Collect the first complete telemetry window before first dispatch.
            next_capture_at = (
                time.monotonic()
                + self.decision_interval
            )

            while self._running:
                wait_seconds = max(
                    0,
                    next_capture_at - time.monotonic(),
                )

                if self._stop_event.wait(wait_seconds):
                    break

                # Freeze exactly one telemetry interval every 20 seconds.
                buffer_snapshot = self.env.swap_buffers()
                recent_feedback = self.feedback.recent_summary()

                if buffer_snapshot:
                    # Gemini processing happens separately and cannot delay
                    # the next 20-second snapshot dispatch.
                    threading.Thread(
                        target=process_snapshot,
                        args=(
                            buffer_snapshot,
                            recent_feedback,
                        ),
                        daemon=True,
                    ).start()

                # Keep the scheduler aligned to a fixed 20-second clock.
                next_capture_at += self.decision_interval

        threading.Thread(
            target=scheduler,
            daemon=True,
        ).start()

    def stop(self):
        self._running = False
        self._stop_event.set()