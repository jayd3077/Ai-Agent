const TOKEN_KEY = "gridcontrol_admin_token";

function getToken() { return sessionStorage.getItem(TOKEN_KEY); }
function setToken(t) { sessionStorage.setItem(TOKEN_KEY, t); }
function clearToken() { sessionStorage.removeItem(TOKEN_KEY); }

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function showCheckingView() {
  document.getElementById("checkingView").hidden = false;
  document.getElementById("loginView").hidden = true;
  document.getElementById("adminView").hidden = true;
}

function showLoginView(message = "") {
  document.getElementById("checkingView").hidden = true;
  document.getElementById("loginView").hidden = false;
  document.getElementById("adminView").hidden = true;
  document.getElementById("loginError").textContent = message;
}

function showAdminView() {
  document.getElementById("checkingView").hidden = true;
  document.getElementById("loginView").hidden = true;
  document.getElementById("adminView").hidden = false;
  loadWarnings();
  loadRules();
  setInterval(loadWarnings, 3000);
}

async function authedFetch(url, options = {}) {
  const token = getToken();
  const headers = Object.assign({}, options.headers, { Authorization: `Bearer ${token}` });
  const res = await fetch(url, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    clearToken();
    showLoginView("Session expired — please sign in again.");
    throw new Error("unauthorized");
  }
  return res;
}

// Verifies a stored token actually works before ever revealing admin
// content. Previously the page trusted "a token exists in sessionStorage"
// as proof of a valid session and showed the Warnings/Rules panels
// immediately (with the login form also still visible until later JS
// re-hid it) — a stale, expired, or forged token could flash real admin
// UI before the 401 check caught up.
async function checkExistingSession() {
  const token = getToken();
  if (!token) {
    showLoginView();
    return;
  }
  try {
    const res = await fetch("/api/admin/donts", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      showAdminView();
    } else {
      clearToken();
      showLoginView();
    }
  } catch (e) {
    clearToken();
    showLoginView("Could not reach the server — please sign in again.");
  }
}

document.getElementById("loginBtn").addEventListener("click", async () => {
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  const errBox = document.getElementById("loginError");
  errBox.textContent = "";
  if (!username || !password) {
    errBox.textContent = "Enter both a user ID and password.";
    return;
  }
  try {
    const res = await fetch("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      errBox.textContent = "Invalid user ID or password.";
      return;
    }
    const data = await res.json();
    setToken(data.token);
    showAdminView();
  } catch (e) {
    errBox.textContent = "Could not reach the server.";
  }
});

async function loadWarnings() {
  try {
    const res = await authedFetch("/api/admin/warnings");
    const data = await res.json();
    const list = document.getElementById("warningsList");
    if (!data.warnings.length) {
      list.innerHTML = '<p class="empty">No open warnings — the agent has stayed within its low-criticality bounds.</p>';
      return;
    }
    list.innerHTML = data.warnings.map(w => `
      <div class="warn-card">
        <div class="warn-reason"><b>${escapeHtml(w.action)}</b> — ${escapeHtml(w.reason)}</div>
        <pre>${escapeHtml(JSON.stringify(w.payload, null, 2))}</pre>
        <div class="warn-actions">
          <button class="approve" data-id="${escapeHtml(w.id)}" data-decision="approved">Approve</button>
          <button class="deny" data-id="${escapeHtml(w.id)}" data-decision="denied">Deny</button>
        </div>
      </div>`).join("");
    list.querySelectorAll("button[data-id]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await authedFetch(`/api/admin/warnings/${btn.dataset.id}/resolve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: btn.dataset.decision }),
        });
        loadWarnings();
      });
    });
  } catch (e) { /* handled in authedFetch */ }
}

async function loadRules() {
  try {
    const res = await authedFetch("/api/admin/donts");
    const data = await res.json();
    const list = document.getElementById("rulesList");
    if (!data.rules.length) {
      list.innerHTML = '<p class="empty">No rules set yet.</p>';
      return;
    }
    list.innerHTML = data.rules.map(r => `
      <div class="rule-row">
        <span>${escapeHtml(r)}</span>
        <button data-rule="${escapeHtml(r)}">Remove</button>
      </div>`).join("");
    list.querySelectorAll("button[data-rule]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await authedFetch("/api/admin/donts", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rule: btn.dataset.rule }),
        });
        loadRules();
      });
    });
  } catch (e) { /* handled in authedFetch */ }
}

document.getElementById("addRuleBtn").addEventListener("click", async () => {
  const input = document.getElementById("newRule");
  const rule = input.value.trim();
  if (!rule) return;
  await authedFetch("/api/admin/donts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rule }),
  });
  input.value = "";
  loadRules();
});

showCheckingView();
checkExistingSession();
