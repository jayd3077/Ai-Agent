"""
explainable.py
---------------
A scoped Q&A layer: answers questions ONLY about this datacenter
simulation/agent (why did it act, what's the current state, what do the
priorities mean, etc). Anything off-topic is rejected *before* it reaches
the LLM, so you don't burn tokens on unrelated chit-chat.

Two-stage guard:
  1. Cheap local keyword/heading check (no API call) -- rejects obviously
     unrelated questions instantly.
  2. If it passes stage 1, the LLM itself is instructed with a strict system
     prompt to refuse anything outside the project's scope, and to answer
     using only the provided state/decision-log context (no external
     knowledge, no general programming/life advice, etc).
"""

from __future__ import annotations
import json
from agent import LLMClient

ON_TOPIC_HINTS = [
    "region", "server", "chip", "chipset", "task", "workload", "temperature",
    "temp", "latency", "carbon", "energy", "price", "electricity", "migrate",
    "migration", "shift", "agent", "decision", "action", "guardrail", "admin",
    "buffer", "feedback", "priority", "datacenter", "data center", "gpu",
    "cost", "bandwidth", "profit", "warning", "lock", "threshold",
]


class ExplainableAI:
    def __init__(self, env, agent, guardrails):
        self.env = env
        self.agent = agent
        self.guardrails = guardrails
        self.llm = LLMClient()

    def _looks_on_topic(self, question: str) -> bool:
        q = question.lower()
        return any(hint in q for hint in ON_TOPIC_HINTS)

    def _context(self) -> dict:
        return {
            "latest_snapshot": list(self.env.frozen_buffer)[-1] if self.env.frozen_buffer else {},
            "recent_decisions": self.agent.decision_log[-5:],
            "open_admin_warnings": [w.__dict__ for w in self.guardrails.open_warnings()],
            "admin_donts": self.guardrails.admin_donts,
        }

    def ask(self, question: str) -> str:
        if not self._looks_on_topic(question):
            return ("I can only answer questions about this datacenter simulation "
                    "and its AI agent (regions, servers, chipsets, tasks, "
                    "temperature/latency/energy decisions, guardrails, etc). "
                    "That question looks out of scope, so I won't call the model for it.")

        system_prompt = """You are the Explainable-AI module for a datacenter
orchestration agent. You answer questions STRICTLY about this project: its
architecture, its current simulated state, why the agent made a particular
decision, what a guardrail/warning means, etc.

Rules:
- Use ONLY the JSON context provided below. Do not use outside knowledge.
- If the question is not about this project (e.g. general coding help,
  unrelated trivia, personal advice), refuse and say you're scoped to this
  project only.
- Be concise (3-6 sentences unless the user asks for detail)."""

        user_prompt = json.dumps({"question": question, "context": self._context()}, indent=2)
        return self.llm.complete(system_prompt, user_prompt)
