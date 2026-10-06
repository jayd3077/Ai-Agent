const ARC = 2 * Math.PI * 18;
let decisionInterval = 20;
// Was hardcoded to 68 and could silently drift out of sync with the
// backend's TEMP_THRESHOLD_C; now kept in sync via /api/state.
let hotThresholdC = 68;

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

const REGION_META = {
  "us-east":  { flag: "🇺🇸", label: "USA — Virginia" },
  "eu-north": { flag: "🇮🇸", label: "Iceland" },
  "eu-west":  { flag: "🇩🇰", label: "Denmark" },
  "ap-south": { flag: "🇮🇳", label: "India — Mumbai" },
  "sa-east":  { flag: "🇧🇷", label: "Brazil — São Paulo" },
};

function chipClass(chip) {
  if (chip.temperature_c >= hotThresholdC) return "hot";
  if (chip.utilization >= 0.60) return "busy";
  if (chip.active_tasks > 0) return "ok";
  return "idle";
}

function renderChip(cid, chip) {
  const cls = chipClass(chip);
  const util = Math.round(chip.utilization * 100);
  const temp = chip.temperature_c.toFixed(0);
  const label = util > 0 ? `${util}%` : "—";

  return `
    <div class="chip-cell ${cls}" title="${cid}&#10;Util: ${util}%&#10;Temp: ${temp}°C&#10;Tasks: ${chip.active_tasks}">
      <span class="chip-util">${label}</span>
      <span class="chip-temp">${temp}°C</span>
      <div class="chip-bar-wrap">
        <div class="chip-bar" style="width:${util}%"></div>
      </div>
    </div>
  `;
}

function renderServer(sid, server) {
  const chips = Object.entries(server.chipsets)
    .map(([cid, chip]) => renderChip(cid, chip))
    .join("");

  const chipsets = Object.values(server.chipsets);

  const avgUtil = Math.round(
    chipsets.reduce((sum, chip) => sum + chip.utilization, 0)
    / chipsets.length
    * 100
  );

  const shortName = sid.split("-").slice(-2).join("-");

  return `
    <div class="server-card">
      <div class="server-header">
        <span class="server-name">${shortName}</span>
        <span class="server-tier-tag ${server.tier}">
          ${server.tier}
        </span>
      </div>

      <div class="chips-grid">${chips}</div>

      <div class="server-util-bar">
        <div
          class="server-util-fill"
          style="width:${avgUtil}%"
        ></div>
      </div>
    </div>
  `;
}

function renderRegion(rid, region) {
  const meta = REGION_META[rid] || {
    flag: "🌐",
    label: rid,
  };

  const cardClass = region.energy_source === "green"
    ? "green-card"
    : "fossil-card";

  const energyTagClass = region.energy_source === "green"
    ? "green"
    : "fossil";

  const servers = Object.entries(region.servers)
    .map(([sid, server]) => renderServer(sid, server))
    .join("");

  return `
    <div class="region-card ${cardClass}">
      <div class="region-header">
        <div class="region-title">
          <span class="region-flag">${meta.flag}</span>

          <div>
            <div class="region-name">${meta.label}</div>
            <div class="region-id">${rid}</div>
          </div>

          <span class="energy-tag ${energyTagClass}">
            ${region.energy_source}
          </span>
        </div>

        <div class="region-kpis">
          <div class="kpi">
            <div class="kpi-val">$${region.electricity_price}</div>
            <div class="kpi-lbl">per kWh</div>
          </div>

          <div class="kpi">
            <div class="kpi-val">${region.carbon_g_per_kwh}</div>
            <div class="kpi-lbl">gCO₂/kWh</div>
          </div>
        </div>
      </div>

      <div class="region-body">
        <div class="servers-grid">${servers}</div>
      </div>
    </div>
  `;
}

function computeGlobalStats(snapshot) {
  let totalTasks = 0;
  let totalTemp = 0;
  let tempCount = 0;

  let cheapest = {
    price: Infinity,
    name: "—",
  };

  let greenest = {
    carbon: Infinity,
    name: "—",
  };

  const regions = snapshot.regions || {};

  for (const [rid, region] of Object.entries(regions)) {
    const meta = REGION_META[rid] || {
      label: rid,
    };

    if (region.electricity_price < cheapest.price) {
      cheapest = {
        price: region.electricity_price,
        name: meta.label.split("—")[0].trim(),
      };
    }

    if (region.carbon_g_per_kwh < greenest.carbon) {
      greenest = {
        carbon: region.carbon_g_per_kwh,
        name: meta.label.split("—")[0].trim(),
      };
    }

    for (const server of Object.values(region.servers || {})) {
      for (const chip of Object.values(server.chipsets || {})) {
        totalTasks += chip.active_tasks;
        totalTemp += chip.temperature_c;
        tempCount++;
      }
    }
  }

  return {
    tasks: totalTasks,
    avgTemp: tempCount
      ? (totalTemp / tempCount).toFixed(1)
      : "—",
    cheapest: cheapest.name,
    greenest: greenest.name,
  };
}

async function pollState() {
  try {
    const response = await fetch("/api/state", {
      cache: "no-store",
    });

    const data = await response.json();

    decisionInterval = data.decision_interval_seconds || 20;
    hotThresholdC = data.temp_threshold_c ?? hotThresholdC;

    const regions = data.snapshot.regions || {};

    document.getElementById("regionsList").innerHTML =
      Object.entries(regions)
        .map(([rid, region]) => renderRegion(rid, region))
        .join("");

    const nextIn = Math.max(
      0,
      data.next_decision_in || 0
    );

    const fraction = nextIn / decisionInterval;
    const offset = ARC * (1 - fraction);

    document.getElementById("timerArc").style.strokeDashoffset =
      offset;

    document.getElementById("timerNum").textContent =
      nextIn;

    const simTime = (data.snapshot.sim_time || "")
      .replace("T", " ")
      .slice(0, 16);

    document.getElementById("simClock").textContent =
      simTime ? `sim ${simTime}` : "";

    const warningCount = Number(
      data.open_warning_count || 0
    );

    const banner = document.getElementById("warnBanner");

    if (warningCount > 0) {
      banner.hidden = false;

      document.getElementById("warnCount").textContent =
        `${warningCount} warning${warningCount > 1 ? "s" : ""}`;
    } else {
      banner.hidden = true;
    }

    const stats = computeGlobalStats(data.snapshot);

    document.getElementById("gTasks").textContent =
      stats.tasks;

    document.getElementById("gTemp").textContent =
      `${stats.avgTemp}°C`;

    document.getElementById("gCheap").textContent =
      stats.cheapest;

    document.getElementById("gGreen").textContent =
      stats.greenest;

    document.getElementById("liveDot").style.background =
      "var(--green)";
  } catch (error) {
    document.getElementById("liveDot").style.background =
      "var(--red)";
  }
}

function tagClass(status) {
  if (status === "executed") return "tag-executed";
  if (status === "passed") return "tag-passed";
  if (status === "blocked_for_admin") return "tag-blocked";
  return "tag-error";
}

function entryClass(status) {
  if (status === "executed") return "executed";
  if (status === "passed") return "passed";
  if (status === "blocked_for_admin") return "blocked";
  return "error";
}

async function pollLog() {
  try {
    const response = await fetch("/api/log", {
      cache: "no-store",
    });

    const data = await response.json();
    const list = document.getElementById("logList");

    if (!data.entries.length) {
      list.innerHTML =
        '<div class="log-empty">Waiting for first decision cycle…</div>';
      return;
    }

    list.innerHTML = data.entries.map((entry) => {
      if (entry.error) {
        return `
          <div class="log-entry error">
            <div class="log-meta">
              <span class="log-action-tag tag-error">ERROR</span>
            </div>
            <div class="log-reasoning">${escapeHtml(entry.error)}</div>
          </div>
        `;
      }

      const status = entry.result?.status || "unknown";
      const action = entry.decision?.action || "—";
      const reasoning = entry.decision?.reasoning || "";
      const time = new Date(
        entry.timestamp * 1000
      ).toLocaleTimeString();

      return `
        <div class="log-entry ${entryClass(status)}">
          <div class="log-meta">
            <span class="log-time">${time}</span>
            <span class="log-action-tag ${tagClass(status)}">
              ${escapeHtml(action)}
            </span>
          </div>
          <div class="log-reasoning">${escapeHtml(reasoning)}</div>
        </div>
      `;
    }).join("");
  } catch (error) {
    // Retry during next scheduled poll.
  }
}

function deltaClass(key, value) {
  if (value === 0 || value === null) {
    return "delta-neutral";
  }

  const lowerIsBetter = [
    "temperature_c",
    "utilization",
    "carbon_g_per_kwh",
    "electricity_price",
  ];

  const good = lowerIsBetter.includes(key)
    ? value < 0
    : value > 0;

  return good
    ? "delta-good"
    : "delta-bad";
}

async function pollFeedback() {
  try {
    const response = await fetch("/api/feedback", {
      cache: "no-store",
    });

    const data = await response.json();
    const list = document.getElementById("feedbackList");

    if (!data.entries.length) {
      list.innerHTML =
        '<div class="log-empty">Outcomes appear ~30s after actions.</div>';
      return;
    }

    list.innerHTML = data.entries.map((entry) => {
      const deltas = entry.delta
        ? Object.entries(entry.delta)
          .map(([key, value]) => {
            const sign = value > 0 ? "+" : "";

            return `
              <span class="delta-chip ${deltaClass(key, value)}">
                ${escapeHtml(key)} ${sign}${value}
              </span>
            `;
          })
          .join("")
        : "";

      return `
        <div class="feedback-entry">
          <div class="feedback-action">
            ${escapeHtml(entry.action)} → ${escapeHtml(entry.target || "—")}
          </div>
          <div class="feedback-deltas">${deltas}</div>
        </div>
      `;
    }).join("");
  } catch (error) {
    // Retry during next scheduled poll.
  }
}

document.getElementById("askBtn").addEventListener(
  "click",
  async () => {
    const input = document.getElementById("askInput");
    const answer = document.getElementById("askAnswer");
    const question = input.value.trim();

    if (!question) return;

    answer.textContent = "Thinking…";
    answer.classList.add("visible");

    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question,
        }),
      });

      const data = await response.json();
      answer.textContent = data.answer;
    } catch (error) {
      answer.textContent = "Could not reach the agent.";
    }
  }
);

document.getElementById("askInput").addEventListener(
  "keydown",
  (event) => {
    if (event.key === "Enter") {
      document.getElementById("askBtn").click();
    }
  }
);

pollState();
pollLog();
pollFeedback();

setInterval(pollState, 1000);
setInterval(pollLog, 2500);
setInterval(pollFeedback, 5000);
