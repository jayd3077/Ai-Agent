"""
main.py
-------
Wires everything together and starts:
  - the 1-second environment tick loop (data flowing in every second)
  - the 5-second agent decision loop (reasoning + action)
Run this in the background (or via the dashboard) before opening viewer.py.
"""

import os
import time
import threading
from dotenv import load_dotenv

# Must run before importing agent.py: agent.py reads TEMP_THRESHOLD_C,
# UTIL_IMBALANCE_THRESHOLD, and MIN_NET_PROFIT as module-level constants at
# import time, so loading .env after that import silently discarded any
# overrides and always fell back to the hardcoded defaults.
load_dotenv()

from environment import DatacenterEnvironment
from guardrails import Guardrails
from feedback import FeedbackLoop
from agent import DatacenterAgent
from explainable import ExplainableAI

BUFFER_WINDOW = int(os.getenv("BUFFER_WINDOW_SECONDS", 5))
DECISION_INTERVAL = int(os.getenv("DECISION_INTERVAL_SECONDS", 5))
FEEDBACK_WINDOW = int(os.getenv("FEEDBACK_WINDOW_SECONDS", 30))
NUM_REGIONS = int(os.getenv("NUM_REGIONS", 3))
SERVERS_PER_REGION = int(os.getenv("SERVERS_PER_REGION", 3))
CHIPSETS_PER_SERVER = int(os.getenv("CHIPSETS_PER_SERVER", 4))


def build_system():
    env = DatacenterEnvironment(
        num_regions=NUM_REGIONS,
        servers_per_region=SERVERS_PER_REGION,
        chipsets_per_server=CHIPSETS_PER_SERVER,
        buffer_window_seconds=BUFFER_WINDOW,
    )
    guardrails = Guardrails()
    feedback = FeedbackLoop(env, window_seconds=FEEDBACK_WINDOW)
    agent = DatacenterAgent(env, guardrails, feedback, decision_interval=DECISION_INTERVAL)
    explainer = ExplainableAI(env, agent, guardrails)
    return env, guardrails, feedback, agent, explainer


def start_tick_loop(env: DatacenterEnvironment):
    def loop():
        while True:
            env.tick(dt=1.0)
            time.sleep(1)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    env, guardrails, feedback, agent, explainer = build_system()
    start_tick_loop(env)
    agent.run_forever()
    print("Datacenter simulation + agent running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop()
