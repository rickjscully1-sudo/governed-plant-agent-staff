/* Ops control center. Talks to /api/* (never auto-approves HIGH actions). Per-visitor session via session.js. */

const STATUS_LABEL = {
  hold: "HOLD",
  hold_soft: "SOFT HOLD",
  hold_hard: "HARD HOLD",
  escalate: "ESCALATE",
  shutdown_pending: "SHUTDOWN PENDING APPROVAL",
  approved: "APPROVED",
  denied: "DENIED",
  other: "REVIEW",
  idle: "READY",
};

const FALLBACK_SCENARIOS = [
  { id: "conflict", label: "Conflict check (possible false alarm)" },
  { id: "true_positive", label: "True-positive bearing failure check" },
  { id: "tool_error_storm", label: "Tool outage (historian/Maximo unavailable)" },
  { id: "stale_data", label: "Stale / unreliable sensor data" },
  { id: "social_engineering", label: "Social engineering (unsigned shutdown demand)" },
  { id: "alert_fatigue", label: "Alert fatigue (repeating nuisance alarms)" },
  { id: "parts_unavailable", label: "Bearing failure but critical spare unavailable" },
  { id: "conflicting_work_orders", label: "Rising vibration vs open PM forbidding stop" },
  { id: "dead_network", label: "Dead sensor vs dead network (private 5G / connectivity)" },
  { id: "unauthorized_plc_write", label: "Unauthorized PLC write / zone-conduit" },
  { id: "maximo_sap_disagree", label: "Maximo vs SAP disagree on work availability" },
  { id: "soft_vs_hard_hold", label: "Soft HOLD vs hard HOLD (investigate while running)" },
];

const PLANT_STORY_IDS = [
  "conflict",
  "true_positive",
  "tool_error_storm",
  "stale_data",
  "social_engineering",
  "alert_fatigue",
  "parts_unavailable",
  "conflicting_work_orders",
];

const OT_PACK_IDS = [
  "dead_network",
  "unauthorized_plc_write",
  "maximo_sap_disagree",
  "soft_vs_hard_hold",
];

let lastReport = null;
let pendingTool = "recommend_line_shutdown";
let scenarios = FALLBACK_SCENARIOS.slice();

function $(id) { return document.getElementById(id); }

function setBusy(on) {
  $("busy").classList.toggle("hidden", !on);
  const ids = ["btn-run", "btn-approve", "btn-deny", "scenario-select"];
  ids.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = on;
  });
  document.querySelectorAll(".scenario-chip").forEach((el) => {
    el.disabled = on;
  });
}

function fillList(el, items, emptyText) {
  el.innerHTML = "";
  const list = (items && items.length) ? items : [emptyText || "None reported"];
  list.forEach((t) => {
    const li = document.createElement("li");
    if (!items || !items.length) li.className = "muted";
    li.textContent = t;
    el.appendChild(li);
  });
}

function applyBanner(report) {
  const banner = $("banner");
  const code = report ? (report.status_code || "other") : "idle";
  banner.className = "status-banner status-" + code;
  $("banner-code").textContent = STATUS_LABEL[code] || code.toUpperCase();
  $("banner-decision").textContent = report ? report.decision : "No assessment running";
  $("banner-meta").textContent = report
    ? (report.site + " · " + report.asset_id + " · " + report.scenario_label)
    : "Choose a scenario above to investigate FILLER-PUMP-A12 on Line 03.";
  $("banner-urgency").textContent = report ? ("Urgency: " + report.urgency) : "Urgency: not assessed";
}

function render(report) {
  lastReport = report;
  applyBanner(report);
  if (!report) return;

  $("f-site").textContent = report.site || "Not available";
  $("f-line").textContent = report.line_id || "Not available";
  $("f-asset").textContent = report.asset_id || "Not available";
  $("f-scenario").textContent = report.scenario_label || report.scenario || "Not available";
  $("f-decision-detail").textContent = report.decision;

  const saw = report.what_we_saw || {};
  fillList($("list-sensors"), saw.sensors);
  fillList($("list-maint"), saw.maintenance);
  fillList($("list-tech"), saw.technician);
  fillList($("list-fleet"), saw.fleet);
  fillList($("list-ot"), saw.ot_connectivity);
  fillList($("list-why"), report.why);

  const ol = $("list-next");
  ol.innerHTML = "";
  (report.next_steps || []).forEach((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    ol.appendChild(li);
  });
  if (!(report.next_steps || []).length) {
    fillList(ol, [], "None reported");
  }

  $("approval-copy").textContent = report.human_approval || "";
  const panel = $("approval-panel");
  const pending = (report.pending_approvals || [])[0];
  // Keep Approve/Deny panel for true_positive pending (and any other pending shutdown)
  if (report.approval_required && pending) {
    panel.classList.remove("hidden");
    pendingTool = pending.tool_name || "recommend_line_shutdown";
    $("approval-action").textContent = "Action requested: " + (pending.action || pendingTool);
  } else {
    panel.classList.add("hidden");
  }

  $("audit-path").textContent = report.audit_path
    ? ("Compliance log (this session only): " + report.audit_path)
    : "Compliance log appears after a run.";
  const audit = $("list-audit");
  audit.innerHTML = "";
  (report.audit_trail || []).forEach((row) => {
    const li = document.createElement("li");
    const time = (row.time || "").replace("T", " ").replace("+00:00", "Z");
    li.innerHTML = "<code>" + escapeHtml(time.slice(0, 19)) + "</code> " + escapeHtml(row.summary || row.type || "");
    audit.appendChild(li);
  });
  if (!(report.audit_trail || []).length) {
    fillList(audit, [], "No audit events");
  }

  if (report.scenario) {
    selectScenario(report.scenario, false);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function post(url, body) {
  const res = await window.demoFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ("Request failed: " + res.status));
  }
  return data;
}

function selectScenario(id, runImmediately) {
  const sel = $("scenario-select");
  if (sel && Array.from(sel.options).some((o) => o.value === id)) {
    sel.value = id;
  }
  document.querySelectorAll(".scenario-chip").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.scenario === id);
  });
  if (runImmediately) runScenario(id);
}

function orderScenarios(list) {
  const byId = {};
  (list || []).forEach((s) => { byId[s.id] = s; });
  const ordered = [];
  const seen = {};
  PLANT_STORY_IDS.concat(OT_PACK_IDS).forEach((id) => {
    if (byId[id]) {
      ordered.push(byId[id]);
      seen[id] = true;
    }
  });
  (list || []).forEach((s) => {
    if (!seen[s.id]) ordered.push(s);
  });
  return ordered;
}

function makeChip(s, isOt) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "scenario-chip btn btn-secondary" + (isOt ? " scenario-chip-ot" : "");
  btn.dataset.scenario = s.id;
  btn.title = s.id;
  const label = document.createElement("span");
  label.className = "chip-label";
  label.textContent = s.label;
  btn.appendChild(label);
  if (isOt) {
    const badge = document.createElement("span");
    badge.className = "chip-badge";
    badge.textContent = "OT";
    btn.appendChild(badge);
  }
  btn.addEventListener("click", () => selectScenario(s.id, true));
  return btn;
}

function populateScenarios(list) {
  scenarios = orderScenarios(list && list.length ? list : FALLBACK_SCENARIOS);
  const sel = $("scenario-select");
  sel.innerHTML = "";
  scenarios.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.label;
    sel.appendChild(opt);
  });
  const grid = $("scenario-grid");
  grid.innerHTML = "";

  const plant = scenarios.filter((s) => PLANT_STORY_IDS.indexOf(s.id) >= 0);
  const ot = scenarios.filter((s) => OT_PACK_IDS.indexOf(s.id) >= 0);
  const other = scenarios.filter(
    (s) => PLANT_STORY_IDS.indexOf(s.id) < 0 && OT_PACK_IDS.indexOf(s.id) < 0
  );

  function addGroup(title, items, isOt) {
    if (!items.length) return;
    const group = document.createElement("div");
    group.className = "scenario-group";
    const heading = document.createElement("h3");
    heading.className = "scenario-group-title";
    heading.textContent = title;
    group.appendChild(heading);
    const chips = document.createElement("div");
    chips.className = "scenario-chips";
    items.forEach((s) => chips.appendChild(makeChip(s, isOt)));
    group.appendChild(chips);
    grid.appendChild(group);
  }

  addGroup("Plant stories", plant, false);
  addGroup("OT / connectivity pack", ot, true);
  addGroup("Other", other, false);

  if (scenarios.length) selectScenario(scenarios[0].id, false);
}

async function runScenario(scenario) {
  setBusy(true);
  try {
    const data = await post("/api/run", { scenario: scenario });
    render(data.report);
  } catch (err) {
    alert("Run failed: " + err.message);
  } finally {
    setBusy(false);
  }
}

async function resolve(approved) {
  if (!lastReport) return;
  setBusy(true);
  try {
    const data = await post("/api/resolve", {
      scenario: lastReport.scenario,
      tool_name: pendingTool,
      approved: !!approved,
    });
    render(data.report);
  } catch (err) {
    alert("Approval failed: " + err.message);
  } finally {
    setBusy(false);
  }
}

$("btn-run").addEventListener("click", () => {
  const id = $("scenario-select").value || "conflict";
  runScenario(id);
});
$("scenario-select").addEventListener("change", () => {
  selectScenario($("scenario-select").value, false);
});
$("btn-approve").addEventListener("click", () => {
  if (confirm(
    "Approve shutdown recommendation for Line 03 Filler?\n\n"
    + "Requires operator + supervisor confirmation (dual-control).\n"
    + "This is an explicit human decision. The agent will not act without it."
  )) {
    resolve(true);
  }
});
$("btn-deny").addEventListener("click", () => resolve(false));

populateScenarios(FALLBACK_SCENARIOS);
window.demoFetch("/api/scenarios")
  .then((r) => r.json())
  .then((d) => {
    if (d && d.ok && d.scenarios && d.scenarios.length) {
      populateScenarios(d.scenarios);
    }
  })
  .catch(() => {});

window.demoFetch("/api/status")
  .then((r) => r.json())
  .then((d) => { if (d.report) render(d.report); })
  .catch(() => {});
