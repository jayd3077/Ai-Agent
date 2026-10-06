"""
feedback.py
-----------
Lightweight feedback loop. Whenever the agent executes an action we record
the *pre*-action metrics for the affected chipset/server/region. N seconds
later we sample the *post*-action metrics and compute deltas (did temp drop?
did utilization even out? did carbon/cost improve?). The resulting log is
fed back into the next LLM prompt as "recent outcomes" so the agent can
see whether its own past decisions actually helped -- a poor man's
in-context reinforcement signal (no weight updates, just better prompting).
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field


@dataclass
class FeedbackEntry:
    action: str
    payload: dict
    pre_metrics: dict
    post_metrics: dict | None = None
    delta: dict | None = None
    created_at: float = field(default_factory=time.time)
    resolved: bool = False


class FeedbackLoop:
    def __init__(self, env, window_seconds: int = 30):
        self.env = env
        self.window_seconds = window_seconds
        self.entries: list[FeedbackEntry] = []
        self._lock = threading.Lock()

    # The keys actually present on action payloads (see agent.py's action
    # parameter requirements). Checked in priority order: prefer the
    # destination id (what the action changed), fall back to the source.
    _TARGET_KEYS = (
        "to_chipset_id", "to_server_id", "to_region_id",
        "from_chipset_id", "from_server_id", "from_region_id",
    )

    @classmethod
    def _target_id(cls, payload: dict):
        for key in cls._TARGET_KEYS:
            value = payload.get(key)
            if value:
                return value
        return None

    def _metrics_for(self, payload: dict) -> dict:
        """Grab a comparable metric snapshot for whatever the action touched."""
        chipset_id = self._target_id(payload)
        if not chipset_id:
            return {}
        for r, s, c in self.env._all_chipsets():
            if c.id == chipset_id or s.id == chipset_id or r.id == chipset_id:
                return {
                    "temperature_c": c.temperature_c,
                    "utilization": c.utilization,
                    "carbon_g_per_kwh": r.carbon_intensity(),
                    "electricity_price": r.electricity_price(self.env.sim_time.hour),
                }
        return {}

    def record(self, action: str, payload: dict):
        entry = FeedbackEntry(action=action, payload=payload,
                                pre_metrics=self._metrics_for(payload))
        with self._lock:
            self.entries.append(entry)

        def _sample_later():
            time.sleep(self.window_seconds)
            entry.post_metrics = self._metrics_for(payload)
            entry.delta = {
                k: round(entry.post_metrics.get(k, 0) - v, 4)
                for k, v in entry.pre_metrics.items()
            }
            entry.resolved = True

        threading.Thread(target=_sample_later, daemon=True).start()

    def recent_summary(self, n: int = 5) -> list[dict]:
        """Feed this straight into the LLM prompt as learning context."""
        with self._lock:
            resolved = [e for e in self.entries if e.resolved][-n:]
        return [
            {"action": e.action, "target": self._target_id(e.payload),
             "delta": e.delta} for e in resolved
        ]
