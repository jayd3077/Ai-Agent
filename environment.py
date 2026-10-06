from __future__ import annotations
import time
import uuid
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# Domain enums / constants
# --------------------------------------------------------------------------

class TaskType(str, Enum):
    IMAGE_GEN = "image_generation"
    TEXT_GEN = "text_generation"
    VIDEO_GEN = "video_generation"
    CODE_GEN = "code_generation"

# Relative compute cost / duration profile per task type (seconds, load units)
TASK_PROFILE = {
    TaskType.IMAGE_GEN: {"duration": (3, 8), "load": (8, 15), "priority": 2},
    TaskType.TEXT_GEN:  {"duration": (1, 4), "load": (3, 8),  "priority": 1},
    TaskType.VIDEO_GEN: {"duration": (10, 30), "load": (20, 35), "priority": 3},
    TaskType.CODE_GEN:  {"duration": (2, 6), "load": (5, 12), "priority": 2},
}

ENERGY_SOURCES = {
    "green": {"carbon_g_per_kwh": 20,  "base_price": 0.09},
    "fossil": {"carbon_g_per_kwh": 550, "base_price": 0.14},
}

# Five real-world-inspired regions with distinct energy profiles, mirroring
# how actual cloud providers differ region to region. This is what makes the
# "shift to a greener/cheaper region" priority meaningful instead of arbitrary.
REGION_PROFILES = [
    {"id": "us-east",  "name": "USA — Virginia",      "energy_source": "fossil",
     "carbon_g_per_kwh": 650, "price_base": 0.12, "ambient_c": 18},
    {"id": "eu-north", "name": "Iceland",              "energy_source": "green",
     "carbon_g_per_kwh": 20,  "price_base": 0.05, "ambient_c": 5},
    {"id": "eu-west",  "name": "Denmark",               "energy_source": "green",
     "carbon_g_per_kwh": 11,  "price_base": 0.28, "ambient_c": 9},
    {"id": "ap-south", "name": "India — Mumbai",        "energy_source": "fossil",
     "carbon_g_per_kwh": 450, "price_base": 0.09, "ambient_c": 28},
    {"id": "sa-east",  "name": "Brazil — São Paulo",     "energy_source": "green",
     "carbon_g_per_kwh": 90,  "price_base": 0.08, "ambient_c": 24},
]


# --------------------------------------------------------------------------
# Core objects
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    type: TaskType
    load: float
    duration: float
    priority: int
    created_at: float
    chipset_id: Optional[str] = None
    server_id: Optional[str] = None
    region_id: Optional[str] = None
    locked: bool = False          # True once assigned -> can't be moved again
    completed: bool = False
    remaining: float = 0.0

    def tick(self, dt: float):
        if not self.completed:
            self.remaining -= dt
            if self.remaining <= 0:
                self.completed = True


@dataclass
class Chipset:
    id: str
    server_id: str
    tier: str                     # "standard" | "powerful"
    capacity: float                # max load units it can hold
    tasks: dict = field(default_factory=dict)   # task_id -> Task
    temperature_c: float = 45.0
    ambient_c: float = 22.0

    @property
    def load(self) -> float:
        return sum(t.load for t in self.tasks.values() if not t.completed)

    @property
    def utilization(self) -> float:
        return min(1.0, self.load / self.capacity) if self.capacity else 0.0

    def update_temperature(self, dt: float):
        # Simple thermal model: heats up under load, cools toward ambient.
        heat_gain = self.utilization * 6.0 * dt
        cooling = (self.temperature_c - self.ambient_c) * 0.08 * dt
        self.temperature_c += heat_gain - cooling
        self.temperature_c = max(self.ambient_c, self.temperature_c)


@dataclass
class Server:
    id: str
    region_id: str
    tier: str                     # "standard" | "powerful" (affects latency)
    chipsets: dict = field(default_factory=dict)  # chipset_id -> Chipset

    @property
    def avg_utilization(self) -> float:
        chips = list(self.chipsets.values())
        return sum(c.utilization for c in chips) / len(chips) if chips else 0.0


@dataclass
class Region:
    id: str
    name: str
    energy_source: str            # "green" | "fossil"
    carbon_g_per_kwh: int = 100
    price_base: float = 0.10
    ambient_c: float = 20.0
    servers: dict = field(default_factory=dict)  # server_id -> Server

    def electricity_price(self, hour: int) -> float:
        """Day/night pricing curve. Peak (8-22h) costs more."""
        is_day = 8 <= hour < 22
        multiplier = 1.35 if is_day else 0.75
        return round(self.price_base * multiplier, 4)

    def carbon_intensity(self) -> int:
        return self.carbon_g_per_kwh


# --------------------------------------------------------------------------
# The environment
# --------------------------------------------------------------------------

class DatacenterEnvironment:
    def __init__(self, num_regions=5, servers_per_region=3, chipsets_per_server=4,
                 buffer_window_seconds=5, sim_seconds_per_real_second=60):
        """
        sim_seconds_per_real_second: how fast the simulated clock advances,
        so day/night pricing cycles are visible in a demo without waiting
        24 real hours. Set to 1 for real-time.
        """
        self.regions: dict[str, Region] = {}
        self.tasks: dict[str, Task] = {}
        self.buffer_window_seconds = buffer_window_seconds
        self.sim_speed = sim_seconds_per_real_second
        self.sim_time = datetime(2026, 1, 1, 6, 0, 0)

        self._lock = threading.RLock()
        self._build_topology(num_regions, servers_per_region, chipsets_per_server)

        # Double buffer: live_buffer fills for the *next* decision window,
        # frozen_buffer is what the agent is currently reasoning over.
        self.live_buffer: deque = deque(maxlen=buffer_window_seconds)
        self.frozen_buffer: deque = deque(maxlen=buffer_window_seconds)
        self.action_log: list[dict] = []

        self._running = False

    # ---- topology setup -------------------------------------------------
    def _build_topology(self, num_regions, servers_per_region, chipsets_per_server):
        profiles = REGION_PROFILES[:num_regions] if num_regions <= len(REGION_PROFILES) \
            else REGION_PROFILES * (num_regions // len(REGION_PROFILES) + 1)
        for profile in profiles[:num_regions]:
            region = Region(id=profile["id"], name=profile["name"],
                             energy_source=profile["energy_source"],
                             carbon_g_per_kwh=profile["carbon_g_per_kwh"],
                             price_base=profile["price_base"],
                             ambient_c=profile["ambient_c"])
            for s in range(servers_per_region):
                sid = f"{region.id}-server-{s}"
                tier = "powerful" if s == 0 else "standard"  # first server per region is the "beefy" one
                server = Server(id=sid, region_id=region.id, tier=tier)
                for c in range(chipsets_per_server):
                    cid = f"{sid}-chip-{c}"
                    capacity = 80 if tier == "powerful" else 45
                    server.chipsets[cid] = Chipset(id=cid, server_id=sid, tier=tier,
                                                     capacity=capacity, ambient_c=region.ambient_c,
                                                     temperature_c=region.ambient_c + 25)
                region.servers[sid] = server
            self.regions[region.id] = region

    # ---- helpers ----------------------------------------------------------
    def _all_chipsets(self):
        for r in self.regions.values():
            for s in r.servers.values():
                for c in s.chipsets.values():
                    yield r, s, c

    def find_chipset(self, chipset_id):
        for r, s, c in self._all_chipsets():
            if c.id == chipset_id:
                return r, s, c
        return None, None, None

    def find_server(self, server_id):
        for r in self.regions.values():
            if server_id in r.servers:
                return r, r.servers[server_id]
        return None, None

    # ---- task generation / tick -------------------------------------------
    def _spawn_task(self):
        ttype = random.choice(list(TaskType))
        profile = TASK_PROFILE[ttype]
        load = random.uniform(*profile["load"])
        duration = random.uniform(*profile["duration"])
        task = Task(
            id=str(uuid.uuid4())[:8],
            type=ttype,
            load=load,
            duration=duration,
            remaining=duration,
            priority=profile["priority"],
            created_at=time.time(),
        )
        # naive placement: random region, prefer least-loaded chipset
        region = random.choice(list(self.regions.values()))
        server = random.choice(list(region.servers.values()))
        chipset = min(server.chipsets.values(), key=lambda c: c.utilization)
        chipset.tasks[task.id] = task
        task.chipset_id, task.server_id, task.region_id = chipset.id, server.id, region.id
        self.tasks[task.id] = task
        return task

    def tick(self, dt=1.0):
        """Advance the simulation by dt seconds. Called once per second."""
        with self._lock:
            self.sim_time += timedelta(seconds=dt * self.sim_speed)

            # spawn 0-3 new tasks arriving this second
            for _ in range(random.randint(0, 3)):
                self._spawn_task()

            # progress every task, update thermal state
            for r, s, c in self._all_chipsets():
                for t in list(c.tasks.values()):
                    t.tick(dt)
                    if t.completed:
                        del c.tasks[t.id]
                        # Also drop it from the global registry so completed
                        # tasks don't accumulate forever (memory leak fix).
                        self.tasks.pop(t.id, None)
                c.update_temperature(dt)

            snapshot = self._snapshot()
            self.live_buffer.append(snapshot)

    def _snapshot(self) -> dict:
        hour = self.sim_time.hour
        regions_state = {}
        for rid, r in self.regions.items():
            servers_state = {}
            for sid, s in r.servers.items():
                chips_state = {
                    cid: {
                        "tier": c.tier,
                        "utilization": round(c.utilization, 3),
                        "temperature_c": round(c.temperature_c, 1),
                        "active_tasks": len(c.tasks),
                        "capacity": c.capacity,
                    } for cid, c in s.chipsets.items()
                }
                servers_state[sid] = {"tier": s.tier, "chipsets": chips_state}
            regions_state[rid] = {
                "name": r.name,
                "energy_source": r.energy_source,
                "electricity_price": r.electricity_price(hour),
                "carbon_g_per_kwh": r.carbon_intensity(),
                "servers": servers_state,
            }
        return {
            "sim_time": self.sim_time.isoformat(),
            "hour": hour,
            "regions": regions_state,
            "pending_tasks": len([t for t in self.tasks.values() if not t.completed]),
        }

    # ---- buffer swap (the "hold 5s while agent decides" mechanism) -------
    def swap_buffers(self):
        """Freeze the current live window for the agent; start a fresh one."""
        with self._lock:
            self.frozen_buffer = self.live_buffer
            self.live_buffer = deque(maxlen=self.buffer_window_seconds)
            return list(self.frozen_buffer)

    # ---- action primitives (called only by the agent's executor) ---------
    def shift_within_server(self, from_chipset_id: str, to_chipset_id: str, task_id: str) -> dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.completed:
                return {"ok": False, "reason": "task not found or already completed"}
            if task.locked:
                return {"ok": False, "reason": "task is locked (already mid-move / in flight)"}
            r_from, s_from, c_from = self.find_chipset(from_chipset_id)
            r_to, s_to, c_to = self.find_chipset(to_chipset_id)
            if not c_from or not c_to or s_from.id != s_to.id:
                return {"ok": False, "reason": "chipsets must exist and be in the same server"}
            if task.chipset_id != from_chipset_id or task_id not in c_from.tasks:
                return {"ok": False, "reason": "task is not currently on from_chipset_id (stale snapshot)"}
            if c_to.utilization >= 0.95 or (c_to.load + task.load) > c_to.capacity:
                return {"ok": False, "reason": "destination chipset has no headroom"}
            del c_from.tasks[task_id]
            c_to.tasks[task_id] = task
            task.chipset_id = to_chipset_id
            task.locked = True  # cannot be re-shifted until it completes
            return {"ok": True, "action": "shift_within_server", "task_id": task_id,
                     "from": from_chipset_id, "to": to_chipset_id}

    def shift_within_region(self, from_server_id: str, to_server_id: str, task_id: str) -> dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.completed:
                return {"ok": False, "reason": "task not found or already completed"}
            if task.locked:
                return {"ok": False, "reason": "task is locked"}
            r_from, s_from = self.find_server(from_server_id)
            r_to, s_to = self.find_server(to_server_id)
            if not s_from or not s_to or r_from.id != r_to.id:
                return {"ok": False, "reason": "servers must exist and be in the same region"}
            if task.server_id != from_server_id:
                return {"ok": False, "reason": "task is not currently on from_server_id (stale snapshot)"}
            c_from = s_from.chipsets.get(task.chipset_id)
            if not c_from or task_id not in c_from.tasks:
                return {"ok": False, "reason": "task's chipset record is inconsistent with from_server_id"}
            c_to = min(s_to.chipsets.values(), key=lambda c: c.utilization)
            if (c_to.load + task.load) > c_to.capacity:
                return {"ok": False, "reason": "destination server has no headroom"}
            del c_from.tasks[task_id]
            c_to.tasks[task_id] = task
            task.chipset_id, task.server_id = c_to.id, to_server_id
            task.locked = True
            return {"ok": True, "action": "shift_within_region", "task_id": task_id,
                     "from": from_server_id, "to": to_server_id}

    def migrate_region(self, from_region_id: str, to_region_id: str, task_id: str,
                        bandwidth_cost_estimate: float, projected_savings: float,
                        min_net_profit: float = 0.0) -> dict:
        with self._lock:
            net_profit = projected_savings - bandwidth_cost_estimate
            # min_net_profit is now an actual enforced floor (configurable via
            # MIN_NET_PROFIT) instead of a hardcoded "> 0" check that ignored it.
            if net_profit <= min_net_profit:
                return {"ok": False,
                        "reason": f"net profit {net_profit:.4f} <= required minimum {min_net_profit}, skipping (pass)",
                        "net_profit": net_profit}
            task = self.tasks.get(task_id)
            if not task or task.completed:
                return {"ok": False, "reason": "task not found or already completed"}
            if task.locked:
                return {"ok": False, "reason": "task is locked"}
            if task.region_id != from_region_id:
                return {"ok": False, "reason": "task is not currently in from_region_id (stale snapshot)"}
            r_from = self.regions.get(from_region_id)
            r_to = self.regions.get(to_region_id)
            if not r_from or not r_to:
                return {"ok": False, "reason": "region not found"}
            c_from = None
            for s in r_from.servers.values():
                if task.chipset_id in s.chipsets and task_id in s.chipsets[task.chipset_id].tasks:
                    c_from = s.chipsets[task.chipset_id]
                    break
            if not c_from:
                return {"ok": False, "reason": "task's chipset record is inconsistent with from_region_id"}
            dest_server = min(r_to.servers.values(), key=lambda s: s.avg_utilization)
            c_to = min(dest_server.chipsets.values(), key=lambda c: c.utilization)
            if (c_to.load + task.load) > c_to.capacity:
                return {"ok": False, "reason": "destination region has no headroom"}
            del c_from.tasks[task_id]
            c_to.tasks[task_id] = task
            task.chipset_id, task.server_id, task.region_id = c_to.id, dest_server.id, to_region_id
            task.locked = True
            return {"ok": True, "action": "migrate_region", "task_id": task_id,
                     "from": from_region_id, "to": to_region_id, "net_profit": net_profit}
