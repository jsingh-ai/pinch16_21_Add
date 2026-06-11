const REFRESH_MS = 15000;
const CENTRAL_TIMEZONE = "America/Chicago";
const centralFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: CENTRAL_TIMEZONE,
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});

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

function formatCentralTime(isoString) {
  if (!isoString) {
    return "Never";
  }
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "Never";
  }
  const parts = centralFormatter.formatToParts(date);
  const partMap = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${partMap.month} ${partMap.day}, ${partMap.year} ${partMap.hour}:${partMap.minute} ${partMap.dayPeriod} CT`;
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) {
    return "Never";
  }
  const totalSeconds = Math.max(0, Math.round(Number(seconds)));
  if (totalSeconds < 60) {
    return `${totalSeconds}s`;
  }
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  }
  return `${minutes}m ${String(remainingSeconds).padStart(2, "0")}s`;
}

function formatMegabytes(value) {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${Number(value).toFixed(2)} MB`;
}

function setActionMessage(message, tone = "warning") {
  const node = document.getElementById("action-message");
  node.textContent = message;
  node.className = `card-subvalue message-${tone}`;
}

function renderOverall(overall) {
  const badge = document.getElementById("overall-status");
  badge.textContent = text(overall.status, "UNKNOWN");
  badge.className = `status-badge ${statusClass(overall.status)}`;
  document.getElementById("overall-message").textContent = text(overall.message);
  document.getElementById("generated-at").textContent = `Generated ${formatCentralTime(overall.generated_at_utc)}`;
  document.getElementById("last-poll-finished").textContent = formatCentralTime(overall.last_poll_finished_at_utc);
  document.getElementById("last-poll-age").textContent = overall.last_poll_age_seconds === null
    ? "Age Never"
    : `Age ${formatAge(overall.last_poll_age_seconds)}`;
  document.getElementById("total-tags").textContent = text(overall.total_enabled_tags, "0");
  document.getElementById("recent-good").textContent = text(overall.recent_good_samples, "0");
  document.getElementById("recent-bad").textContent = text(overall.recent_bad_samples, "0");
  document.getElementById("poll-duration").textContent =
    overall.latest_poll_duration_seconds === null ? "-" : formatAge(overall.latest_poll_duration_seconds);
  document.getElementById("storage-size").textContent = formatMegabytes(overall.db_size_mb);
  document.getElementById("storage-retention").textContent =
    `Good ${text(overall.sample_retention_days, "0")}d | Bad ${text(overall.bad_sample_retention_days, "0")}d | Polls ${text(overall.poll_run_retention_days, "0")}d`;
  document.getElementById("storage-counts").textContent =
    `Samples ${text(overall.total_sample_rows, "0")} | Good ${text(overall.total_good_sample_rows, "0")} | Bad ${text(overall.total_bad_sample_rows, "0")} | Poll runs ${text(overall.poll_run_count, "0")}`;
  document.getElementById("storage-range").textContent =
    `Oldest ${formatCentralTime(overall.oldest_sample_utc)} | Newest ${formatCentralTime(overall.newest_sample_utc)}`;
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
        <div class="muted">${formatCentralTime(machine.latest_sample_utc)}</div>
      </td>
      <td><span class="status-badge ${statusClass(machine.status)}">${text(machine.status)}</span></td>
      <td class="endpoint">${text(machine.endpoint_url)}</td>
      <td>${text(machine.enabled_tags, "0")}</td>
      <td>${formatCentralTime(machine.latest_sample_utc)}</td>
      <td>${formatAge(machine.latest_sample_age_seconds)}</td>
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
      <td>${formatCentralTime(run.started_at_utc)}</td>
      <td>${formatCentralTime(run.finished_at_utc)}</td>
      <td>${run.duration_seconds === null ? "-" : formatAge(run.duration_seconds)}</td>
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
      <div class="error-meta">${formatCentralTime(error.timestamp_utc)} | ${text(error.machine_name)} | age ${formatAge(error.age_seconds)}</div>
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
    db_size_mb: 0,
    total_sample_rows: 0,
    total_good_sample_rows: 0,
    total_bad_sample_rows: 0,
    poll_run_count: 0,
    recent_good_samples: 0,
    recent_bad_samples: 0,
    latest_poll_duration_seconds: null,
    sample_retention_days: 0,
    bad_sample_retention_days: 0,
    poll_run_retention_days: 0,
  });
  renderMachineStrip([]);
  renderMachines([]);
  renderPollRuns([]);
  renderErrors([]);
}

function setActionButtonsDisabled(disabled) {
  document.querySelectorAll(".action-button").forEach((button) => {
    button.disabled = disabled;
  });
}

async function postAction(endpoint, confirmMessage = null) {
  if (confirmMessage) {
    const confirmed = window.confirm(confirmMessage);
    if (!confirmed) {
      return { cancelled: true };
    }
  }

  setActionButtonsDisabled(true);
  const actionName = endpoint.split("/").pop();
  setActionMessage(`Running ${actionName}...`, "warning");

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(confirmMessage ? { confirm: true } : {}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      setActionMessage(payload.error || `Action failed: ${actionName}`, "critical");
      return payload;
    }
    const resultSummary = payload.deleted || payload.result || {};
    setActionMessage(`${actionName} completed: ${JSON.stringify(resultSummary)}`, "good");
    await refreshDashboard();
    return payload;
  } catch (error) {
    setActionMessage(`Action failed: ${actionName}`, "critical");
    return { ok: false };
  } finally {
    setActionButtonsDisabled(false);
  }
}

function bindActionButtons() {
  document.querySelectorAll(".action-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const actionName = button.dataset.action;
      const confirmMessage = actionName === "cleanup-now"
        ? null
        : `Confirm action: ${actionName}. This does not delete machines or tags.`;
      await postAction(`/api/actions/${actionName}`, confirmMessage);
    });
  });
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
bindActionButtons();
setInterval(refreshDashboard, REFRESH_MS);
