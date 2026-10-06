"""
guardrails.py
-------------
Draws the hard line between what the autonomous agent is allowed to do
and what requires a human Admin.

LOW-CRITICALITY (agent may execute autonomously):
  - shift_within_server
  - shift_within_region
  - migrate_region  (only when net profit > 0)

HIGH-CRITICALITY (agent may only *propose*, never execute):
  - shutdown_server / shutdown_region
  - move_entire_server (bulk migration of every task on a server)
  - anything an Admin adds to the "don't" list at runtime

When the agent's reasoning step outputs a high-criticality action, or an
action that matches an Admin "don't" rule, it is converted into a
WARNING that surfaces on the Admin tab instead of being executed.
"""

from __future__ import annotations
import re
import time
import json
from dataclasses import dataclass, field, asdict

# Common connector words that shouldn't count as "meaningful" tokens when
# matching an admin free-text rule against an action. Without this, a rule
# like "never migrate out of eu-west" could fire on unrelated actions just
# because their payload happened to contain "out" or "of" as a substring.
_RULE_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "into", "out", "off",
    "never", "don't", "dont", "do", "not", "should", "must", "always",
    "any", "all", "for", "and", "or",
}

LOW_CRITICALITY_ACTIONS = {
    "shift_within_server",
    "shift_within_region",
    "migrate_region",
    "pass",  # explicit no-op is always allowed
}

# NOTE: The agent's LLM is intentionally never given these as valid choices
# (see agent.py's DECISION_SCHEMA_HINT / valid_actions) — it can only ever
# propose shift_within_server, shift_within_region, migrate_region, or pass.
# This set exists as a safety net / extension point in case a future,
# broader action vocabulary is introduced; evaluate() will still correctly
# route any of these to an admin warning rather than letting them execute,
# it's just that nothing in the current agent can actually produce them.
HIGH_CRITICALITY_ACTIONS = {
    "shutdown_server",
    "shutdown_region",
    "move_entire_server",
    "move_entire_region",
    "delete_task",
}


@dataclass
class Warning:
    id: str
    action: str
    payload: dict
    reason: str
    created_at: float = field(default_factory=time.time)
    resolved: bool = False
    admin_decision: str | None = None  # "approved" | "denied" | None


class Guardrails:
    def __init__(self, store_path: str = "guardrails_state.json"):
        self.store_path = store_path
        self.admin_donts: list[str] = []   # free-text rules, e.g. "never migrate out of eu-west"
        self.warnings: list[Warning] = []
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self):
        try:
            with open(self.store_path) as f:
                data = json.load(f)
                self.admin_donts = data.get("admin_donts", [])
        except FileNotFoundError:
            pass

    def _save(self):
        with open(self.store_path, "w") as f:
            json.dump({"admin_donts": self.admin_donts}, f, indent=2)

    # ---- admin API -----------------------------------------------------
    def add_dont(self, rule: str):
        self.admin_donts.append(rule)
        self._save()

    def remove_dont(self, rule: str):
        if rule in self.admin_donts:
            self.admin_donts.remove(rule)
            self._save()

    def resolve_warning(self, warning_id: str, decision: str) -> "Warning | None":
        """Marks the warning resolved and returns it (so the caller can act
        on an 'approved' decision), or None if no such warning exists."""
        for w in self.warnings:
            if w.id == warning_id:
                w.resolved = True
                w.admin_decision = decision
                return w
        return None

    # ---- the actual gate -------------------------------------------------
    def evaluate(self, action_name: str, payload: dict) -> dict:
        """
        Returns one of:
          {"verdict": "allow"}
          {"verdict": "pass"}                     # agent chose not to act
          {"verdict": "block", "warning": Warning} # needs Admin
        """
        if action_name == "pass":
            return {"verdict": "pass"}

        if action_name in HIGH_CRITICALITY_ACTIONS:
            w = self._raise_warning(action_name, payload,
                                     "High-criticality action requires Admin approval.")
            return {"verdict": "block", "warning": w}

        # Check against admin-defined free-text don'ts. This is still a
        # keyword heuristic (not real semantic understanding), but it now
        # requires ALL meaningful words of the rule to appear as whole words
        # (not substrings) in the action name + its payload, rather than ANY
        # single word. That avoids two failure modes of the old check:
        #   - over-blocking: a rule containing a common word like "out" or
        #     "servers" used to match almost any action's payload.
        #   - under-blocking: a rule phrased with a word form that never
        #     appears verbatim (e.g. "migrations" vs. the action name
        #     "migrate_region") used to never match at all.
        # The action name is folded into the searchable text (with
        # underscores turned into spaces) so rules can refer to the action
        # itself, e.g. "never migrate to ap-south".
        blob = (action_name.replace("_", " ") + " " + json.dumps(payload)).lower()
        for rule in self.admin_donts:
            tokens = [t for t in re.findall(r"[a-z0-9\-]+", rule.lower())
                      if t not in _RULE_STOPWORDS]
            if tokens and all(re.search(rf"\b{re.escape(tok)}\b", blob) for tok in tokens):
                w = self._raise_warning(action_name, payload,
                                         f"Matches admin rule: '{rule}'")
                return {"verdict": "block", "warning": w}

        if action_name not in LOW_CRITICALITY_ACTIONS:
            w = self._raise_warning(action_name, payload, "Unrecognized action type.")
            return {"verdict": "block", "warning": w}

        return {"verdict": "allow"}

    def _raise_warning(self, action_name, payload, reason) -> Warning:
        w = Warning(id=str(int(time.time() * 1000)), action=action_name,
                     payload=payload, reason=reason)
        self.warnings.append(w)
        return w

    def open_warnings(self):
        return [w for w in self.warnings if not w.resolved]
