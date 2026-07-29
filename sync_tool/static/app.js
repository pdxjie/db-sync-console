const state = {
  tables: [],
  selected: new Set(),
  currentRunId: null,
  pollTimer: null,
  connectionReady: false,
  desktop: Boolean(window.dbSyncDesktop?.isDesktop),
};

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "pause_requested"]);
const RESUMABLE_RUN_STATUSES = new Set(["failed", "paused"]);

const $ = (id) => document.getElementById(id);

const els = {
  connectionSummary: $("connectionSummary"),
  connectionState: $("connectionState"),
  saveConnectionsBtn: $("saveConnectionsBtn"),
  loginConnectionsBtn: $("loginConnectionsBtn"),
  testProdBtn: $("testProdBtn"),
  testTestBtn: $("testTestBtn"),
  prodHost: $("prodHost"),
  prodPort: $("prodPort"),
  prodUser: $("prodUser"),
  prodPassword: $("prodPassword"),
  prodDatabase: $("prodDatabase"),
  prodCharset: $("prodCharset"),
  testHost: $("testHost"),
  testPort: $("testPort"),
  testUser: $("testUser"),
  testPassword: $("testPassword"),
  testDatabase: $("testDatabase"),
  testCharset: $("testCharset"),
  refreshAllBtn: $("refreshAllBtn"),
  reloadTablesBtn: $("reloadTablesBtn"),
  tableSearch: $("tableSearch"),
  tableList: $("tableList"),
  tableError: $("tableError"),
  selectedCount: $("selectedCount"),
  selectVisibleBtn: $("selectVisibleBtn"),
  clearSelectionBtn: $("clearSelectionBtn"),
  mode: $("mode"),
  batchSize: $("batchSize"),
  dryRun: $("dryRun"),
  createMissingTables: $("createMissingTables"),
  syncStrategy: $("syncStrategy"),
  cursorField: $("cursorField"),
  incrementalField: $("incrementalField"),
  incrementalSince: $("incrementalSince"),
  shardCount: $("shardCount"),
  workerCount: $("workerCount"),
  skipExactCount: $("skipExactCount"),
  whereClause: $("whereClause"),
  planBtn: $("planBtn"),
  startBtn: $("startBtn"),
  planSummary: $("planSummary"),
  planWarnings: $("planWarnings"),
  planBody: $("planBody"),
  jobName: $("jobName"),
  scheduleEnabled: $("scheduleEnabled"),
  cronExpr: $("cronExpr"),
  saveJobBtn: $("saveJobBtn"),
  reloadJobsBtn: $("reloadJobsBtn"),
  jobList: $("jobList"),
  reloadRunsBtn: $("reloadRunsBtn"),
  runHistory: $("runHistory"),
  runStatus: $("runStatus"),
  runMetrics: $("runMetrics"),
  pauseBtn: $("pauseBtn"),
  resumeBtn: $("resumeBtn"),
  progressBar: $("progressBar"),
  runTables: $("runTables"),
  runLogs: $("runLogs"),
  toast: $("toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => els.toast.classList.add("hidden"), 2600);
}

function formatNumber(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toLocaleString("zh-CN");
}

function formatGb(value) {
  if (value === null || value === undefined) return "-";
  return `${Number(value).toFixed(4)} GB`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const value = Math.max(0, Number(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = Math.floor(value % 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function syncStrategyLabel(strategy, cursorField = "", workerCount = 1) {
  if (strategy === "cursor") return `大表并发 ${cursorField || "自动游标"} | ${workerCount || 1} 并发`;
  if (strategy === "offset") return "强制 offset";
  return `智能同步${cursorField ? ` | ${cursorField}` : ""}`;
}

function runStatusLabel(status) {
  const labels = {
    queued: "排队中",
    running: "同步中",
    pause_requested: "暂停中",
    paused: "已暂停",
    success: "已完成",
    failed: "失败",
  };
  return labels[status] || status || "-";
}

function filteredTables() {
  const query = els.tableSearch.value.trim().toLowerCase();
  if (!query) return state.tables;
  return state.tables.filter((item) => item.name.toLowerCase().includes(query));
}

async function loadStatus() {
  const status = await api("/api/status");
  const config = status.config;
  state.connectionReady = Boolean(status.connection_ready);
  populateConnectionForms(status.connections || {});
  els.connectionState.textContent = state.connectionReady ? "已登录" : "未登录";
  els.connectionState.className = state.connectionReady ? "status-pill ready" : "status-pill";
  const sourceText = config.config_exists ? "含高级配置" : "页面连接";
  els.connectionSummary.textContent = `${sourceText} | ${config.prod.database || "prod"} @ ${config.prod.host || "-"} -> ${config.test.database || "test"} @ ${config.test.host || "-"}`;
}

async function loadTables() {
  els.tableError.textContent = "";
  if (!state.connectionReady) {
    state.tables = [];
    els.tableList.innerHTML = '<div class="table-item"><span></span><span class="muted">请先登录数据库连接</span><span></span></div>';
    updateSelectedCount();
    return;
  }
  els.tableList.innerHTML = '<div class="table-item"><span></span><span class="muted">加载中...</span><span></span></div>';
  try {
    const payload = await api("/api/tables");
    state.tables = payload.tables || [];
    renderTables();
  } catch (error) {
    state.tables = [];
    renderTables();
    els.tableError.textContent = error.message;
  }
}

function populateConnectionForms(connections) {
  fillConnection("prod", connections.prod);
  fillConnection("test", connections.test);
}

function fillConnection(env, data) {
  if (!data) return;
  const prefix = env === "prod" ? "prod" : "test";
  els[`${prefix}Host`].value = data.host || "";
  els[`${prefix}Port`].value = data.port || 3306;
  els[`${prefix}User`].value = data.user || "";
  els[`${prefix}Database`].value = data.database || "";
  els[`${prefix}Charset`].value = data.charset || "utf8mb4";
  els[`${prefix}Password`].placeholder = data.password_set ? "留空沿用已保存密码" : "";
}

function getConnectionPayload(env) {
  const prefix = env === "prod" ? "prod" : "test";
  const port = Number.parseInt(els[`${prefix}Port`].value, 10);
  return {
    host: els[`${prefix}Host`].value.trim(),
    port: Number.isFinite(port) ? port : 3306,
    user: els[`${prefix}User`].value.trim(),
    password: els[`${prefix}Password`].value,
    database: els[`${prefix}Database`].value.trim(),
    charset: els[`${prefix}Charset`].value.trim() || "utf8mb4",
    connect_timeout: 10,
    read_timeout: 120,
    write_timeout: 120,
  };
}

function getConnectionsPayload() {
  return {
    prod: getConnectionPayload("prod"),
    test: getConnectionPayload("test"),
  };
}

async function saveConnections() {
  setBusy(true);
  try {
    await api("/api/connections", {
      method: "POST",
      body: JSON.stringify(getConnectionsPayload()),
    });
    clearConnectionPasswords();
    await loadStatus();
    showToast("连接已保存");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function loginConnections() {
  setBusy(true);
  try {
    await api("/api/connections/login", {
      method: "POST",
      body: JSON.stringify(getConnectionsPayload()),
    });
    clearConnectionPasswords();
    await loadStatus();
    await loadTables();
    showToast("登录成功");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function testConnection(env) {
  setBusy(true);
  try {
    const payload = { env, connection: getConnectionPayload(env) };
    const result = await api("/api/connections/test", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast(`${env === "prod" ? "产品库" : "测试库"}连接成功：${result.result.database}`);
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

function clearConnectionPasswords() {
  els.prodPassword.value = "";
  els.testPassword.value = "";
}

function renderTables() {
  const tables = filteredTables();
  els.tableList.innerHTML = "";
  if (!tables.length) {
    els.tableList.innerHTML = '<div class="table-item"><span></span><span class="muted">无匹配表</span><span></span></div>';
    updateSelectedCount();
    return;
  }
  for (const table of tables) {
    const row = document.createElement("label");
    row.className = "table-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(table.name);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(table.name);
      else state.selected.delete(table.name);
      updateSelectedCount();
    });
    const name = document.createElement("span");
    name.className = "table-name";
    name.textContent = table.name;
    name.title = table.name;
    const count = document.createElement("span");
    count.className = "row-count";
    count.textContent = formatNumber(table.estimated_rows);
    row.append(checkbox, name, count);
    els.tableList.append(row);
  }
  updateSelectedCount();
}

function updateSelectedCount() {
  els.selectedCount.textContent = `已选 ${state.selected.size}`;
}

function getPayload() {
  const batchSize = Number.parseInt(els.batchSize.value, 10);
  const shardCount = Number.parseInt(els.shardCount.value, 10);
  const workerCount = Number.parseInt(els.workerCount.value, 10);
  return {
    tables: Array.from(state.selected),
    mode: els.mode.value,
    where_clause: els.whereClause.value.trim(),
    batch_size: Number.isFinite(batchSize) ? batchSize : null,
    create_missing_tables: els.createMissingTables.checked,
    sync_strategy: els.syncStrategy.value,
    cursor_field: els.cursorField.value.trim(),
    incremental_field: els.incrementalField.value.trim(),
    incremental_since: els.incrementalSince.value.trim(),
    skip_exact_count: els.skipExactCount.checked,
    shard_count: Number.isFinite(shardCount) ? shardCount : 1,
    worker_count: Number.isFinite(workerCount) ? workerCount : 1,
    dry_run: els.dryRun.checked,
    name: els.jobName.value.trim() || null,
  };
}

async function buildPlan() {
  const payload = getPayload();
  if (!payload.tables.length) {
    showToast("请选择表");
    return;
  }
  setBusy(true);
  try {
    const plan = await api("/api/plan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderPlan(plan);
    showToast("计划已生成");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

function renderPlan(plan) {
  els.planSummary.textContent = `${plan.table_count} 张表，${formatNumber(plan.total_rows)} 行，分页 ${plan.batch_size}`;
  els.planWarnings.innerHTML = "";
  for (const warning of plan.warnings || []) {
    const item = document.createElement("div");
    item.className = "warning-item";
    item.textContent = warning;
    els.planWarnings.append(item);
  }
  els.planBody.innerHTML = "";
  for (const table of plan.tables || []) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(table.name)}</td>
      <td>${escapeHtml(table.action)}</td>
      <td>${escapeHtml(table.pagination_strategy === "cursor" ? "keyset" : table.pagination_strategy || "-")}</td>
      <td>${formatNumber(table.row_count)}${table.estimated ? " 估算" : ""}</td>
      <td>${formatNumber(table.shard_count || 1)}</td>
      <td>${escapeHtml(table.cursor_field || (table.primary_keys || []).join(", ") || "-")}</td>
    `;
    els.planBody.append(row);
  }
}

async function startRun() {
  const payload = getPayload();
  if (!payload.tables.length) {
    showToast("请选择表");
    return;
  }
  setBusy(true);
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.currentRunId = run.id;
    renderRun(run);
    pollRun();
    showToast("已加入队列");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function saveJob() {
  const payload = getPayload();
  const name = els.jobName.value.trim();
  if (!name) {
    showToast("请输入任务名称");
    return;
  }
  if (!payload.tables.length) {
    showToast("请选择表");
    return;
  }
  const job = {
    name,
    tables: payload.tables,
    mode: payload.mode,
    where_clause: payload.where_clause,
    batch_size: payload.batch_size,
    create_missing_tables: payload.create_missing_tables,
    sync_strategy: payload.sync_strategy,
    cursor_field: payload.cursor_field,
    incremental_field: payload.incremental_field,
    incremental_since: payload.incremental_since,
    skip_exact_count: payload.skip_exact_count,
    shard_count: payload.shard_count,
    worker_count: payload.worker_count,
    schedule_enabled: els.scheduleEnabled.checked,
    cron_expr: els.cronExpr.value.trim(),
  };
  setBusy(true);
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify(job) });
    await loadJobs();
    showToast("任务已保存");
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

async function loadJobs() {
  const payload = await api("/api/jobs");
  renderJobs(payload.jobs || []);
}

function renderJobs(jobs) {
  els.jobList.innerHTML = "";
  if (!jobs.length) {
    els.jobList.innerHTML = '<div class="muted">暂无任务</div>';
    return;
  }
  for (const job of jobs) {
    const item = document.createElement("div");
    item.className = "job-item";
    item.innerHTML = `
      <div class="job-line">
        <strong>${escapeHtml(job.name)}</strong>
        <span class="muted">${escapeHtml(job.mode)}</span>
      </div>
      <div class="muted">${escapeHtml(job.tables.join(", "))}</div>
      <div class="muted">${escapeHtml(syncStrategyLabel(job.sync_strategy, job.cursor_field, job.worker_count))}</div>
      <div class="muted">${job.create_missing_tables ? "缺表自动建表" : "缺表时报错"}</div>
      <div class="muted">${job.schedule_enabled ? `cron ${escapeHtml(job.cron_expr)}` : "手动"}</div>
      <div class="job-actions">
        <button class="secondary" data-action="run">运行</button>
        <button class="secondary" data-action="load">载入</button>
        <button class="secondary" data-action="delete">删除</button>
      </div>
    `;
    item.querySelector('[data-action="run"]').addEventListener("click", () => runJob(job.id));
    item.querySelector('[data-action="load"]').addEventListener("click", () => loadJobIntoForm(job));
    item.querySelector('[data-action="delete"]').addEventListener("click", () => deleteJob(job.id));
    els.jobList.append(item);
  }
}

function loadJobIntoForm(job) {
  state.selected = new Set(job.tables);
  els.mode.value = job.mode;
  els.whereClause.value = job.where_clause || "";
  els.batchSize.value = job.batch_size || 5000;
  els.createMissingTables.checked = Boolean(job.create_missing_tables);
  els.syncStrategy.value = job.sync_strategy || "auto";
  els.cursorField.value = job.cursor_field || "";
  els.incrementalField.value = job.incremental_field || "";
  els.incrementalSince.value = job.incremental_since || "";
  els.skipExactCount.checked = Boolean(job.skip_exact_count);
  els.shardCount.value = job.shard_count || 1;
  els.workerCount.value = job.worker_count || 1;
  els.jobName.value = job.name;
  els.scheduleEnabled.checked = Boolean(job.schedule_enabled);
  els.cronExpr.value = job.cron_expr || "";
  renderTables();
  showToast("任务已载入");
}

async function runJob(jobId) {
  try {
    const run = await api(`/api/jobs/${jobId}/run`, { method: "POST" });
    state.currentRunId = run.id;
    renderRun(run);
    pollRun();
    showToast("任务已启动");
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteJob(jobId) {
  try {
    await api(`/api/jobs/${jobId}`, { method: "DELETE" });
    await loadJobs();
    showToast("任务已删除");
  } catch (error) {
    showToast(error.message);
  }
}

async function loadRuns() {
  const payload = await api("/api/runs");
  renderRunHistory(payload.runs || []);
}

function renderRunHistory(runs) {
  els.runHistory.innerHTML = "";
  if (!runs.length) {
    els.runHistory.innerHTML = '<div class="muted">暂无历史</div>';
    return;
  }
  for (const run of runs) {
    const item = document.createElement("button");
    item.className = "history-item";
    item.innerHTML = `
      <div class="history-line">
        <strong>${escapeHtml(run.name)}</strong>
        <span>${escapeHtml(runStatusLabel(run.status))}</span>
      </div>
      <div class="muted">${escapeHtml(run.created_at)}</div>
    `;
    item.addEventListener("click", () => openRun(run.id));
    els.runHistory.append(item);
  }
}

async function openRun(runId) {
  try {
    state.currentRunId = runId;
    const run = await api(`/api/runs/${runId}?logs_limit=120`);
    renderRun(run);
    if (ACTIVE_RUN_STATUSES.has(run.status)) pollRun();
  } catch (error) {
    showToast(error.message);
  }
}

async function pollRun() {
  window.clearTimeout(state.pollTimer);
  if (!state.currentRunId) return;
  try {
    const run = await api(`/api/runs/${state.currentRunId}?logs_limit=80`);
    renderRun(run);
    if (ACTIVE_RUN_STATUSES.has(run.status)) {
      state.pollTimer = window.setTimeout(pollRun, run.status === "pause_requested" ? 700 : 1400);
    } else {
      await loadRuns();
    }
  } catch (error) {
    showToast(error.message);
  }
}

function renderRun(run) {
  const percent = run.total_rows > 0 ? Math.round((run.processed_rows / run.total_rows) * 100) : run.status === "success" ? 100 : 0;
  els.runStatus.textContent = `${runStatusLabel(run.status)} | ${formatNumber(run.processed_rows)}/${formatNumber(run.total_rows)} | ${percent}%`;
  renderRunMetrics(run);
  els.progressBar.style.width = `${Math.min(100, percent)}%`;
  els.pauseBtn.classList.toggle("hidden", !ACTIVE_RUN_STATUSES.has(run.status));
  els.pauseBtn.disabled = run.status === "pause_requested";
  els.pauseBtn.textContent = run.status === "pause_requested" ? "暂停中" : "暂停";
  els.pauseBtn.onclick = () => pauseRun(run.id);
  els.resumeBtn.classList.toggle("hidden", !RESUMABLE_RUN_STATUSES.has(run.status));
  els.resumeBtn.onclick = () => resumeRun(run.id);

  els.runTables.innerHTML = "";
  for (const table of run.tables_state || []) {
    const item = document.createElement("div");
    item.className = "run-table-item";
    const tablePercent = table.total_rows > 0 ? Math.round((table.processed_rows / table.total_rows) * 100) : table.status === "success" ? 100 : 0;
    item.innerHTML = `
      <div class="run-table-line">
        <strong>${escapeHtml(table.table_name)}</strong>
        <span>${escapeHtml(runStatusLabel(table.status))} ${tablePercent}%</span>
      </div>
      <div class="muted">${formatNumber(table.processed_rows)} / ${formatNumber(table.total_rows)} rows, ${table.cursor_field ? `last_pk ${escapeHtml(table.last_pk || "-")}` : `offset ${formatNumber(table.offset_value)}`}</div>
      ${table.error ? `<div class="error-text">${escapeHtml(table.error)}</div>` : ""}
    `;
    els.runTables.append(item);
  }

  els.runLogs.textContent = (run.logs || [])
    .map((item) => `${item.created_at} [${item.level.toUpperCase()}] ${item.message}`)
    .join("\n");
  els.runLogs.scrollTop = els.runLogs.scrollHeight;
}

function renderRunMetrics(run) {
  const metrics = [
    ["速度", `${formatNumber(run.rows_per_second || 0)} 行/秒`],
    ["已同步", formatGb(run.synced_gb || 0)],
    ["预计剩余", formatDuration(run.eta_seconds)],
    ["耗时", formatDuration(run.elapsed_seconds || 0)],
  ];
  els.runMetrics.innerHTML = metrics
    .map(([label, value]) => `<div class="metric-card"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
}

async function resumeRun(runId) {
  try {
    const run = await api(`/api/runs/${runId}/resume`, { method: "POST" });
    renderRun(run);
    pollRun();
    showToast("已继续");
  } catch (error) {
    showToast(error.message);
  }
}

async function pauseRun(runId) {
  try {
    const run = await api(`/api/runs/${runId}/pause`, { method: "POST" });
    renderRun(run);
    pollRun();
    showToast("已请求暂停，当前批次完成后会停住");
  } catch (error) {
    showToast(error.message);
  }
}

function setBusy(isBusy) {
  els.saveConnectionsBtn.disabled = isBusy;
  els.loginConnectionsBtn.disabled = isBusy;
  els.testProdBtn.disabled = isBusy;
  els.testTestBtn.disabled = isBusy;
  els.planBtn.disabled = isBusy;
  els.startBtn.disabled = isBusy;
  els.saveJobBtn.disabled = isBusy;
  els.pauseBtn.disabled = isBusy;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bindEvents() {
  els.saveConnectionsBtn.addEventListener("click", saveConnections);
  els.loginConnectionsBtn.addEventListener("click", loginConnections);
  els.testProdBtn.addEventListener("click", () => testConnection("prod"));
  els.testTestBtn.addEventListener("click", () => testConnection("test"));
  els.refreshAllBtn.addEventListener("click", refreshAll);
  els.reloadTablesBtn.addEventListener("click", loadTables);
  els.tableSearch.addEventListener("input", renderTables);
  els.selectVisibleBtn.addEventListener("click", () => {
    for (const table of filteredTables()) state.selected.add(table.name);
    renderTables();
  });
  els.clearSelectionBtn.addEventListener("click", () => {
    state.selected.clear();
    renderTables();
  });
  els.planBtn.addEventListener("click", buildPlan);
  els.startBtn.addEventListener("click", startRun);
  els.saveJobBtn.addEventListener("click", saveJob);
  els.reloadJobsBtn.addEventListener("click", loadJobs);
  els.reloadRunsBtn.addEventListener("click", loadRuns);
}

async function refreshAll() {
  try {
    await loadStatus();
    const tasks = [loadJobs(), loadRuns()];
    if (state.connectionReady) tasks.push(loadTables());
    else loadTables();
    await Promise.allSettled(tasks);
  } catch (error) {
    showToast(error.message);
  }
}

bindEvents();
if (state.desktop) {
  document.body.classList.add("desktop-app");
}
refreshAll();
