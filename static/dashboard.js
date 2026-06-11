const REFRESH_MS = 15000;

function statusClass(status) {
  const normalized = (status || "UNKNOWN").toLowerCase();
  if (normalized === "good") return "status-good";
  if (normalized === "warning") return "status-warning";
  if (normalized === "critical") return "status-critical";
  return "status-unknown";
}

function text(value, fallback = "-") {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  return String(value);
}

function formatSeconds(value) {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${Number(value).toFixed(1)}s`;
}

function renderOverall(overall) {
  const badge = document.getElementById("overall-status");
  badge.textContent = text(overall.status, "UNKNOWN");
  badge.className = `status-badge ${statusClass(overall.status)}`;
  document.getElementById("overall-message").textContent = text(overall.message);
  document.getElementById("generated-at").textContent = `Generated ${text(overall.generated_at_utc)}`;
  document.getElementById("last-poll-finished").textContent = text(overall.last_poll_finished_at_utc);
  document.getElementById("last-poll-age").textContent = overall.last_poll_age_seconds === null
    ? "-"
    : `Age ${formatSeconds(overall.last_poll_age_seconds)}`;
  document.getElementById("total-tags").textContent = text(overall.total_enabled_tags, "0");
  document.getElementById("recent-good").textContent = text(overall.recent_good_samples, "0");
  document.getElementById("recent-bad").textContent = text(overall.recent_bad_samples, "0");
  document.getElementById("poll-duration").textContent = formatSeconds(overall.latest_poll_duration_seconds);
}

function renderMachineStrip(machines) {
  const strip = document.getElementById("machine-strip");
  strip.innerHTML = "";
  if (!machines.length) {
    strip.innerHTML = '<div class="empty-state">No enabled machines found.</div>';
    return;
  }
  machines.forEach((machine) => {
    const item = document.createElement("div");
    item.className = "strip-item";
    item.innerHTML = `
      <div class="strip-name">${text(machine.machine_name)}</div>
      <div class="status-badge ${statusClass(machine.status)}">${text(machine.status)}</div>
      <div class="muted">${text(machine.status_reason)}</div>
    `;
    strip.appendChild(item);
  });
}

function renderMachines(machines) {
  const body = document.getElementById("machine-table-body");
  body.innerHTML = "";
  if (!machines.length) {
    body.innerHTML = '<tr><td colspan="12" class="empty-state">No machine status data available.</td></tr>';
    return;
  }
  machines.forEach((machine) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>
        <strong>${text(machine.machine_name)}</strong>
        <div class="muted">${text(machine.last_updated_display, "Never")}</div>
      </td>
      <td><span class="status-badge ${statusClass(machine.status)}">${text(machine.status)}</span></td>
      <td class="endpoint">${text(machine.endpoint_url)}</td>
      <td>${text(machine.enabled_tags, "0")}</td>
      <td>${text(machine.latest_sample_utc, "Never")}</td>
      <td>${formatSeconds(machine.latest_sample_age_seconds)}</td>
      <td>${text(machine.recent_good_samples, "0")}</td>
      <td>${text(machine.recent_bad_samples, "0")}</td>
      <td>${text(machine.recent_success_rate, "0")}%</td>
      <td>${text(machine.last_poll_attempted_tags, "0")}</td>
      <td>${text(machine.last_poll_failed_tags, "0")}</td>
      <td class="error-text">${text(machine.top_recent_error, machine.status_reason)}</td>
    `;
    body.appendChild(row);
  });
}

function renderPollRuns(runs) {
  const body = document.getElementById("poll-runs-body");
  body.innerHTML = "";
  if (!runs.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty-state">No poll history yet.</td></tr>';
    return;
  }
  runs.forEach((run) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${text(run.started_at_utc)}</td>
      <td>${text(run.finished_at_utc)}</td>
      <td>${formatSeconds(run.duration_seconds)}</td>
      <td>${text(run.machines_ok, "0")}/${text(run.machines_attempted, "0")}</td>
      <td>${text(run.tags_ok, "0")}/${text(run.tags_attempted, "0")}</td>
      <td>${text(run.tags_failed, "0")}</td>
    `;
    body.appendChild(row);
  });
}

function renderErrors(errors) {
  const container = document.getElementById("recent-errors");
  container.innerHTML = "";
  if (!errors.length) {
    container.innerHTML = '<div class="empty-state">No recent bad samples.</div>';
    return;
  }
  errors.forEach((error) => {
    const item = document.createElement("div");
    item.className = "error-item";
    item.innerHTML = `
      <div class="error-meta">${text(error.timestamp_utc)} | ${text(error.machine_name)} | age ${formatSeconds(error.age_seconds)}</div>
      <div><strong>${text(error.tag_name)}</strong></div>
      <div class="node-id">${text(error.node_id)}</div>
      <div class="error-text">${text(error.error_text)}</div>
    `;
    container.appendChild(item);
  });
}

function renderCriticalFallback(message) {
  renderOverall({
    status: "CRITICAL",
    message,
    generated_at_utc: new Date().toISOString(),
    last_poll_finished_at_utc: null,
    last_poll_age_seconds: null,
    total_enabled_tags: 0,
    recent_good_samples: 0,
    recent_bad_samples: 0,
    latest_poll_duration_seconds: null,
  });
  renderMachineStrip([]);
  renderMachines([]);
  renderPollRuns([]);
  renderErrors([]);
}

async function refreshDashboard() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      renderCriticalFallback(payload?.overall?.message || "Dashboard cannot read DB.");
      return;
    }
    renderOverall(payload.overall || {});
    renderMachineStrip(payload.machines || []);
    renderMachines(payload.machines || []);
    renderPollRuns(payload.recent_poll_runs || []);
    renderErrors(payload.recent_errors || []);
  } catch (error) {
    renderCriticalFallback("Dashboard cannot read DB.");
  }
}

refreshDashboard();
setInterval(refreshDashboard, REFRESH_MS);
