/* Agent Staff view. Simulation / mock plant data. Talks to /api/staff/*.
   Approve / Deny are explicit human clicks, one named role at a time. */

(function () {
  "use strict";

  var STAFF_FALLBACK = [
    { id: "filler_drift_to_work_order", label: "Filler vibration drift to drafted work order" },
    { id: "part_gap_escalation", label: "Part gap: Maximo and SAP disagree, fail closed" },
    { id: "co2_out_of_spec_hold", label: "CO2 below spec: hold needs QA and Shift Lead" },
    { id: "out_of_lane_guardrails", label: "Guardrail check: agents reach outside their lane" },
    { id: "stale_sensor_fail_closed", label: "Stale sensor data: stop and escalate" },
    { id: "policy_tampered_fail_closed", label: "Policy edited after sign-off: reads only, fail closed" },
  ];
  var TIER_LABEL = { read: "READ", draft: "DRAFT", production: "2 HUMANS", blocked: "BLOCKED" };
  var STATUS_CLASS = {
    escalate: "status-escalate",
    partial: "status-shutdown_pending",
    pending: "status-shutdown_pending",
    approved: "status-approved",
    blocked: "status-hold",
    denied: "status-denied",
    clear: "status-hold",
    policy_blocked: "status-escalate",
  };
  var ROLE_TITLES = {};
  var staffScenarios = STAFF_FALLBACK.slice();
  var stories = {};
  var current = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function fill(ul, items, empty) {
    ul.innerHTML = "";
    if (!items || !items.length) {
      ul.appendChild(el("li", "muted", empty || "None."));
      return;
    }
    items.forEach(function (t) { ul.appendChild(el("li", null, t)); });
  }

  /* ---------- tabs ---------- */
  function showView() {
    var staff = (location.hash || "").replace("#", "") === "staff";
    $("view-staff").classList.toggle("hidden", !staff);
    $("view-assessment").classList.toggle("hidden", staff);
    $("tab-staff").classList.toggle("active", staff);
    $("tab-assessment").classList.toggle("active", !staff);
  }
  window.addEventListener("hashchange", showView);

  function busy(on, text) {
    var b = $("busy");
    if (b) {
      b.textContent = text || "Working...";
      b.classList.toggle("hidden", !on);
    }
    ["staff-btn-run", "staff-scenario-select"].forEach(function (id) { if ($(id)) $(id).disabled = on; });
    document.querySelectorAll(".staff-chip, .staff-decide").forEach(function (n) { n.disabled = on; });
  }

  async function api(method, url, body) {
    var res = await window.demoFetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
    });
    var data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || ("Request failed: " + res.status));
    return data;
  }

  /* ---------- control policy ---------- */
  function renderPolicy(p) {
    if (!p) return;
    var signed = !!p.signed;
    var card = $("staff-card-policy");
    card.classList.toggle("policy-signed", signed);
    card.classList.toggle("policy-unsigned", !signed);
    $("policy-name").textContent = p.name || "Control policy";
    $("policy-label").textContent = p.label || "";
    var st = $("policy-status");
    st.textContent = p.status_label || (signed ? "Signed and active" : "Not signed, blocked");
    st.className = "policy-status " + (signed ? "policy-status-ok" : "policy-status-bad");
    var blocked = $("policy-blocked");
    blocked.textContent = (p.blocked_message || "Policy not signed: drafts and production actions blocked")
      + ((p.problems || []).length ? " " + p.problems.join(" ") : "");
    blocked.classList.toggle("hidden", signed);
    $("policy-owner").textContent = (p.owner && p.owner.label) || "No owner on file";
    $("policy-effective").textContent = p.effective_date || "-";
    var sig = $("policy-signoffs");
    sig.innerHTML = "";
    (p.signoffs || []).forEach(function (s) {
      var li = el("li");
      li.appendChild(el("span", "policy-sig-check", signed ? "SIGNED" : "SIGNATURE VOID"));
      li.appendChild(document.createTextNode(" " + s.name + " (" + s.title + "), " + s.signed_on));
      sig.appendChild(li);
    });
    if (!(p.signoffs || []).length) sig.appendChild(el("li", "muted", "No leadership sign-off on file."));
    $("policy-fingerprint").textContent = "sha256:" + (p.fingerprint || "?");
    $("policy-fp-note").textContent = signed
      ? "matches signed fingerprint"
      : "signed fingerprint was sha256:" + (p.signed_fingerprint || "none");
    $("policy-changelog").textContent = (p.change_log || []).map(function (c) {
      return "v" + c.version + " (" + c.date + "): " + c.summary;
    }).join(" | ") || "-";
    fill($("policy-rules"), p.rules_compact, "No rules loaded.");
    var lanes = $("policy-lanes");
    lanes.innerHTML = "";
    (p.lanes || []).forEach(function (l) {
      var li = el("li", "policy-lane");
      li.appendChild(el("span", "policy-lane-name", l.name));
      var tools = el("span", "staff-tools policy-lane-tools");
      (l.tools || []).forEach(function (t) {
        var chip = el("span", "staff-tool tier-" + t.tier);
        chip.title = t.tool + (t.approvers && t.approvers.length ? " | approver: " + t.approvers.join(" + ") : "");
        chip.appendChild(el("b", null, TIER_LABEL[t.tier] || t.tier));
        chip.appendChild(document.createTextNode(" " + t.tool));
        tools.appendChild(chip);
      });
      if (!(l.tools || []).length) tools.appendChild(el("span", "muted small", "No tools (fail closed)"));
      li.appendChild(tools);
      lanes.appendChild(li);
    });
    if (p.simulation_label) {
      $("policy-foot").textContent = p.simulation_label + " Fictional people. Every audit entry and approval records the policy version and fingerprint it ran under.";
    }
  }

  /* ---------- org ---------- */
  function renderOrg(org) {
    if (org.simulation_label) $("staff-sim-label").textContent = org.simulation_label;
    if (org.plant) $("staff-plant").textContent = org.plant;
    (org.humans || []).forEach(function (h) { ROLE_TITLES[h.role_id] = h.title + " (" + h.name + ")"; });
    var wrap = $("staff-org");
    wrap.innerHTML = "";
    (org.agents || []).forEach(function (a) {
      var card = el("div", "staff-agent" + (a.agent_id === "coordinator" ? " staff-agent-coord" : ""));
      card.appendChild(el("div", "staff-agent-name", a.name));
      card.appendChild(el("div", "staff-agent-job", a.job));
      var tools = el("div", "staff-tools");
      (a.tools || []).forEach(function (t) {
        var chip = el("span", "staff-tool tier-" + t.tier);
        chip.title = t.tool + (t.approvers && t.approvers.length ? " | approver: " + t.approvers.join(" + ") : "");
        chip.appendChild(el("b", null, TIER_LABEL[t.tier] || t.tier));
        chip.appendChild(document.createTextNode(" " + t.label));
        tools.appendChild(chip);
      });
      card.appendChild(tools);
      card.appendChild(el("div", "staff-agent-approver", "Human: " + a.approver));
      card.appendChild(el("div", "staff-agent-never", a.never));
      wrap.appendChild(card);
    });
    var legend = $("staff-legend");
    legend.innerHTML = "";
    (org.tiers || []).forEach(function (t) {
      var row = el("div", "staff-legend-row");
      row.appendChild(el("span", "staff-tool tier-" + t.tier, TIER_LABEL[t.tier] || t.tier));
      row.appendChild(el("span", null, t.rule));
      legend.appendChild(row);
    });
  }

  function renderChips() {
    var sel = $("staff-scenario-select");
    var chips = $("staff-chips");
    sel.innerHTML = "";
    chips.innerHTML = "";
    staffScenarios.forEach(function (s) {
      var o = el("option", null, s.label);
      o.value = s.id;
      sel.appendChild(o);
      var b = el("button", "scenario-chip btn btn-secondary staff-chip");
      b.type = "button";
      b.dataset.scenario = s.id;
      b.appendChild(el("span", "chip-label", s.label));
      b.addEventListener("click", function () { run(s.id); });
      chips.appendChild(b);
    });
    markActive(sel.value);
  }

  function markActive(id) {
    var sel = $("staff-scenario-select");
    if (id) sel.value = id;
    document.querySelectorAll(".staff-chip").forEach(function (b) {
      b.classList.toggle("active", b.dataset.scenario === id);
    });
    $("staff-story").textContent = stories[id] || $("staff-story").textContent;
  }

  /* ---------- state ---------- */
  function render(state) {
    current = state;
    if (!state) return;
    markActive(state.scenario);
    if (state.story) $("staff-story").textContent = state.story;

    var o = state.outcome || {};
    $("staff-banner").className = "status-banner " + (STATUS_CLASS[o.code] || "status-idle");
    $("staff-banner-code").textContent = o.label || "REVIEW";
    $("staff-banner-headline").textContent = o.headline || "";
    $("staff-banner-meta").textContent = state.label + " | " + state.simulation_label;
    var open = (state.approvals || []).filter(function (a) { return a.status === "pending"; }).length;
    $("staff-banner-count").textContent = open + " awaiting humans | " + (state.blocked || []).length + " blocked";

    if (state.policy) renderPolicy(state.policy);
    renderApprovals(state.approvals || []);
    renderContrib(state);

    var blocked = $("staff-blocked");
    blocked.innerHTML = "";
    if (!(state.blocked || []).length) blocked.appendChild(el("li", "muted", "No blocked attempts."));
    (state.blocked || []).forEach(function (b) {
      var li = el("li", "staff-blocked-item");
      li.appendChild(el("span", "staff-badge badge-blocked", "BLOCKED"));
      if (b.injected) li.appendChild(el("span", "staff-badge badge-injected", "INJECTED TEST"));
      li.appendChild(el("span", null, " " + b.message));
      blocked.appendChild(li);
    });

    var esc = [];
    (state.conflicts || []).forEach(function (c) { esc.push("Conflict: " + c.detail); });
    (state.escalations || []).forEach(function (e) {
      esc.push("Escalated to " + e.to_labels.join(" + ") + ". Ask: " + e.action);
    });
    fill($("staff-escalations"), esc, "No conflicts. No escalations.");

    renderBriefs(state.briefs || []);
    var h = state.handoff || {};
    fill($("staff-h-happened"), h.happened, "-");
    fill($("staff-h-open"), h.open, "-");
    fill($("staff-h-risky"), h.risky, "-");

    $("staff-audit-path").textContent = (state.audit_path ? "Audit log (this session only): " + state.audit_path + " | " : "")
      + "Hash chain verified: " + (state.audit_ok ? "yes" : "NO");
    var audit = $("staff-audit");
    audit.innerHTML = "";
    (state.audit_trail || []).forEach(function (r) {
      var li = el("li");
      var t = r.time ? new Date(r.time) : null;
      li.appendChild(el("code", null, t ? t.toLocaleTimeString() : ""));
      if (r.policy) {
        var tag = el("span", "staff-policy-tag" + (r.policy_signed === false ? " unsigned" : ""), r.policy);
        tag.title = "Policy fingerprint sha256:" + (r.policy_fingerprint || "?");
        li.appendChild(document.createTextNode(" "));
        li.appendChild(tag);
      }
      li.appendChild(document.createTextNode(" " + r.summary));
      audit.appendChild(li);
    });
  }

  function renderApprovals(list) {
    var wrap = $("staff-approvals");
    wrap.innerHTML = "";
    if (!list.length) {
      wrap.appendChild(el("p", "muted", "No requests. Nothing is waiting on a human."));
      return;
    }
    list.forEach(function (a) {
      var card = el("div", "staff-approval status-" + a.status);
      var head = el("div", "staff-approval-head");
      head.appendChild(el("span", "staff-tool tier-" + a.tier, TIER_LABEL[a.tier] || a.tier));
      head.appendChild(el("span", "staff-approval-id", a.approval_id));
      head.appendChild(el("span", "staff-approval-status", a.status.toUpperCase()));
      var ptag = el("span", "staff-policy-tag", a.policy_label || "Policy unknown");
      ptag.title = "Raised under " + (a.policy_id || "policy") + " v" + (a.policy_version || "?")
        + ", fingerprint sha256:" + (a.policy_fingerprint || "?");
      head.appendChild(ptag);
      head.appendChild(el("span", "staff-progress" + (a.dual_control ? " dual" : ""), a.progress + (a.dual_control ? " (dual control)" : "")));
      card.appendChild(head);
      card.appendChild(el("div", "staff-approval-summary", a.summary));
      card.appendChild(el("div", "muted small", "Requested by " + (a.requested_by_name || a.requested_by) + " | " + a.tool_label));
      if (a.status === "held") {
        card.appendChild(el("div", "staff-held", "HELD, cannot be approved: " + a.hold_reason));
      }
      if (a.status === "executed" && a.result) {
        card.appendChild(el("div", "staff-executed", "Executed (mock): " + (a.result.message || "")));
      }
      var roles = el("div", "staff-roles");
      (a.required || []).forEach(function (r) {
        var row = el("div", "staff-role-row");
        row.appendChild(el("span", "staff-role-name", r.label));
        if (r.decision) {
          row.appendChild(el("span", "staff-badge " + (r.decision === "approved" ? "badge-ok" : "badge-blocked"), r.decision.toUpperCase()));
        } else if (a.can_decide) {
          var ok = el("button", "btn btn-danger staff-decide", "Approve as " + r.label.split(" (")[0]);
          ok.type = "button";
          ok.addEventListener("click", function () { decide(a, r, true); });
          var no = el("button", "btn btn-secondary staff-decide", "Deny");
          no.type = "button";
          no.addEventListener("click", function () { decide(a, r, false); });
          row.appendChild(ok);
          row.appendChild(no);
        } else {
          row.appendChild(el("span", "muted", a.status === "held" ? "Locked (fail closed)" : "No decision"));
        }
        roles.appendChild(row);
      });
      card.appendChild(roles);
      wrap.appendChild(card);
    });
  }

  function renderContrib(state) {
    var routes = $("staff-routes");
    routes.innerHTML = "";
    (state.routes || []).forEach(function (r) {
      routes.appendChild(el("li", null, "Coordinator to " + r.to_name + ": " + r.reason));
    });
    var wrap = $("staff-contrib");
    wrap.innerHTML = "";
    (state.contributions || []).forEach(function (c) {
      var card = el("div", "staff-contrib-card st-" + c.status);
      var head = el("div", "staff-contrib-head");
      head.appendChild(el("span", "staff-contrib-name", c.name));
      head.appendChild(el("span", "staff-badge badge-" + c.status, c.status.toUpperCase()));
      card.appendChild(head);
      card.appendChild(el("div", "staff-contrib-headline", c.headline));
      var ul = el("ul", "plain-list small");
      (c.findings || []).forEach(function (f) { ul.appendChild(el("li", null, f)); });
      card.appendChild(ul);
      if (c.recommendation) card.appendChild(el("div", "staff-contrib-rec", "Recommends: " + c.recommendation));
      if (c.tools_used && c.tools_used.length) {
        card.appendChild(el("div", "muted small", "Tools used: " + c.tools_used.join(", ")));
      }
      wrap.appendChild(card);
    });
  }

  function renderBriefs(briefs) {
    var wrap = $("staff-briefs");
    wrap.innerHTML = "";
    if (!briefs.length) { wrap.appendChild(el("p", "muted", "No briefs.")); return; }
    briefs.forEach(function (b) {
      var card = el("div", "staff-brief");
      card.appendChild(el("div", "staff-brief-role", b.title + " (" + b.name + ")"));
      if ((b.needs_decision || []).length) {
        card.appendChild(el("h3", null, "Needs your decision"));
        var u1 = el("ul", "plain-list");
        b.needs_decision.forEach(function (n) { u1.appendChild(el("li", "staff-needs", n.text)); });
        card.appendChild(u1);
      }
      if ((b.escalated || []).length) {
        card.appendChild(el("h3", null, "Escalated to you"));
        var u2 = el("ul", "plain-list");
        b.escalated.forEach(function (t) { u2.appendChild(el("li", "staff-esc", t)); });
        card.appendChild(u2);
      }
      card.appendChild(el("h3", null, "For your awareness"));
      var u3 = el("ul", "plain-list small");
      (b.fyi || []).forEach(function (t) { u3.appendChild(el("li", null, t)); });
      card.appendChild(u3);
      wrap.appendChild(card);
    });
  }

  /* ---------- actions ---------- */
  async function run(id) {
    markActive(id);
    busy(true, "Running staff scenario...");
    $("staff-decision-msg").classList.add("hidden");
    try {
      var d = await api("POST", "/api/staff/run", { scenario: id });
      render(d.state);
    } catch (err) {
      alert("Staff run failed: " + err.message);
    } finally {
      busy(false);
    }
  }

  async function decide(a, r, approved) {
    if (approved && a.tier === "production") {
      var ok = confirm(
        "Approve as " + r.label + "?\n\n" + a.summary + "\n\n"
        + "This is production-affecting. It needs two different humans. "
        + "Your approval alone does not execute it."
      );
      if (!ok) return;
    }
    busy(true, "Recording decision...");
    try {
      var d = await api("POST", "/api/staff/decide", { approval_id: a.approval_id, role: r.role, approved: !!approved });
      render(d.state);
      var msg = $("staff-decision-msg");
      msg.textContent = (d.result && d.result.message) || "";
      msg.classList.toggle("hidden", !msg.textContent);
    } catch (err) {
      alert("Decision failed: " + err.message);
    } finally {
      busy(false);
    }
  }

  $("staff-btn-run").addEventListener("click", function () { run($("staff-scenario-select").value); });
  $("staff-scenario-select").addEventListener("change", function () { markActive($("staff-scenario-select").value); });

  showView();
  renderChips();
  window.demoFetch("/api/staff/policy").then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.ok && !(current && current.policy)) renderPolicy(d.policy);
  }).catch(function () {});
  window.demoFetch("/api/staff/org").then(function (r) { return r.json(); }).then(function (d) {
    if (!d || !d.ok) return;
    renderOrg(d.org);
    if (d.scenarios && d.scenarios.length) {
      staffScenarios = d.scenarios;
      d.scenarios.forEach(function (s) { stories[s.id] = s.story; });
      renderChips();
    }
    return window.demoFetch("/api/staff/state").then(function (r) { return r.json(); }).then(function (s) {
      if (s && s.state) render(s.state);
    });
  }).catch(function () {});
})();
