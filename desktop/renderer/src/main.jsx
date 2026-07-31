import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  App as AntApp,
  Badge,
  Button,
  Checkbox,
  ConfigProvider,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Layout,
  List,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  theme,
} from "antd";
import {
  ApiOutlined,
  BookOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloudSyncOutlined,
  DatabaseOutlined,
  DownOutlined,
  FieldTimeOutlined,
  FolderOpenOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SaveOutlined,
  SearchOutlined,
  SettingOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import "antd/dist/reset.css";
import "./styles.css";

const { Header, Sider, Content } = Layout;
const { Text, Title } = Typography;
const APP_NAME = "同步犬";
const APP_LOGO_URL = new URL("../../../assets/app-icon.png", import.meta.url).href;
const BIG_TABLE_BATCH_SIZE = 5000;
const RUN_STREAM_STATUSES = new Set(["queued", "running", "pause_requested", "cancel_requested"]);
const RUN_PAUSABLE_STATUSES = new Set(["queued", "running", "pause_requested"]);
const RUN_CANCELABLE_STATUSES = new Set(["queued", "running", "pause_requested", "paused", "cancel_requested"]);
const RUN_RESUMABLE_STATUSES = new Set(["failed", "paused"]);
const DEFAULT_SYNC_VALUES = {
  mode: "replace",
  batch_size: 5000,
  create_missing_tables: false,
  sync_strategy: "auto",
  cursor_field: "",
  incremental_field: "",
  incremental_since: "",
  skip_exact_count: false,
  shard_count: 2,
  worker_count: 2,
  where_clause: "",
};
const LEFT_PANE_WIDTH_KEY = "syncdog.leftPaneWidth";
const LEFT_PANE_MIN_WIDTH = 300;
const LEFT_PANE_MAX_WIDTH = 680;
const HELP_TEXT = {
  connection:
    "填写产品库和测试库连接。产品库建议使用只读账号；测试库账号需要写入权限。保存会保留连接信息，测试并登录会立即拉取线上表。",
  refresh: "重新读取连接状态、任务、历史和表信息。修改数据库结构或连接后可以点这里刷新。",
  tableSearch: "只过滤左侧列表显示，不会清空已勾选的表。换关键词后，之前勾选过的表仍会参与同步。",
  selectVisible: "勾选当前搜索结果里的全部表，适合按前缀或关键字批量选择。",
  clearTables: "清空所有已勾选表，不只清空当前搜索结果。",
  refreshTables: "重新从产品库读取表列表和估算行数。",
  strategy: "智能同步会优先使用主键游标；强制 offset 使用传统 LIMIT/OFFSET；大表并发使用游标分片和多个 worker。",
  plan:
    "只做预检和生成同步计划，不写入测试库。会显示表、字段匹配、行数估算、读取策略、游标和分片信息。",
  dryRun:
    "创建一次运行记录并按真实流程检查读取、字段映射和 SQL 生成，但不向测试库写入数据，适合正式同步前验证。",
  start:
    "开始把产品库中已选择表的数据写入测试库。同步过程中可以继续选择其他表启动新任务；同一张表会自动排队等待。",
  mode:
    "replace 使用 REPLACE INTO，主键或唯一键冲突时先删除旧行再插入；upsert 使用 INSERT ... ON DUPLICATE KEY UPDATE，只更新冲突行字段。",
  batchSize:
    "每批读取和写入的行数。值越大吞吐可能越高，但单批事务和内存压力也更大。大表通常从 3000 到 10000 试起。",
  cursor:
    "大表模式的分页游标。默认自动使用主键；没有主键时可填唯一、稳定、递增的字段，例如 id。",
  incrementalField:
    "增量同步字段，常用 updated_at。填写后会和增量起点组合，只同步之后变化的数据。",
  incrementalSince:
    "增量起始时间，例如 2026-07-01 00:00:00。只有同时填写增量字段时才生效。",
  shardCount: "把游标范围拆成多少个分片。分片越多越利于并发，但过多会增加数据库压力。",
  workerCount: "同时执行同步的 worker 数。一般不要超过数据库可承受连接数，建议 2 到 4 起步。",
  createMissing:
    "测试库缺表时自动按产品库结构创建。若测试库已有表，会保留测试库字段，只同步两边共有且可写入的字段。",
  skipCount: "跳过 SELECT COUNT(*) 精确统计，减少大表预检耗时。进度会使用估算值或运行中累计值。",
  where:
    "追加到每张表读取 SQL 的过滤条件，不需要写 WHERE 关键字。例如 created_at >= '2026-07-01'。",
  planPanel: "生成计划后在这里检查每张表的动作、字段数量、行数估算、读取方式、游标和分片。",
  runPanel: "显示多个运行任务的状态、进度、速度、已同步数据量、预计剩余时间、表进度、分片进度和本地日志。不同表可并行，同表会自动排队。",
  runBatch: "批量操作当前活动任务。暂停会等批次提交后停住；取消后不能继续，已经提交到测试库的数据不会自动回滚。",
  pause: "请求暂停。为了避免半批次写入，会等当前批次提交或结束后停住，之后可以继续。",
  cancel: "取消当前任务。不会直接强杀正在执行的 MySQL SQL，会在下一个批次检查点停住；取消后这个运行记录不能继续。",
  resume: "继续已暂停或失败的任务。大表模式会从 last_pk 断点继续，普通模式会从记录的进度继续。",
  jobName: "给当前同步配置命名，保存后可在任务列表中一键载入或运行。",
  schedule: "开启后按 cron 表达式定时运行此任务。不开启时只是保存常用任务。",
  cron: "cron 表达式，例如 0 2 * * * 表示每天 02:00 执行。",
  saveJob: "保存当前选表、同步设置、where 条件和定时配置。",
  jobLoad: "把保存过的任务配置重新载入到当前界面，便于修改或手动运行。",
  jobRun: "直接按保存的任务配置启动一次同步，不需要重新勾选表。",
  history: "查看历史运行记录。点击一条历史可以打开详情、日志和断点状态。",
  prodConnection: "产品库连接。建议用只读账号，避免工具拥有线上写权限。",
  testConnection: "测试库连接。需要 INSERT、UPDATE、DELETE 权限；如启用缺表自动建表，还需要 CREATE 权限。",
  testButton: "只测试当前连接是否可用，不保存另一侧配置。",
};
const DOC_SECTIONS = [
  {
    title: "快速开始",
    items: [
      "点击右上角连接，填写产品库和测试库。产品库建议使用只读账号，测试库需要写入权限。",
      "测试并登录成功后，左侧会显示产品库表列表。搜索表名并勾选要同步的表。",
      "在中间配置写入模式、分页大小、where 条件和大表相关参数。",
      "先点生成计划检查影响范围，再点 Dry-run 验证流程，最后点开始同步正式写入测试库。同步中可以继续选择其他表发起新任务。",
    ],
  },
  {
    title: "选表和搜索",
    items: [
      "搜索只影响左侧显示结果，不会取消已经勾选的表。你换关键词继续勾选时，之前选中的表仍会保留。",
      "选择当前会勾选当前搜索结果里的全部表。清空会取消所有已选表。",
      "表名过长时会单行省略，鼠标悬停可看完整表名；左侧边缘可以拖宽。",
    ],
  },
  {
    title: "写入模式",
    items: [
      "replace 覆盖：使用 REPLACE INTO。遇到主键或唯一键冲突时，MySQL 会删除旧行再插入新行。",
      "upsert 插入或更新：使用 INSERT ... ON DUPLICATE KEY UPDATE。冲突时只更新目标字段，更适合保留目标表行状态。",
      "当产品库和测试库字段不一致时，工具会保留测试库结构，只同步两边共有且可写入的字段。",
    ],
  },
  {
    title: "读取策略和大表",
    items: [
      "智能同步：优先选择主键游标，适合大多数表。没有合适游标时才退回 offset。",
      "强制 offset：使用 LIMIT/OFFSET。大表越往后越慢，只建议小表或临时排查使用。",
      "大表并发：使用游标字段拆分区间，多个 worker 并发同步，并把断点记录为 last_pk。",
      "游标字段应唯一、稳定、最好递增。默认使用主键；没有主键时可以手动填唯一 id 类字段。",
    ],
  },
  {
    title: "过滤和增量",
    items: [
      "where 条件会追加到读取 SQL 中，不需要写 WHERE 关键字。所有勾选表都会使用同一条件。",
      "增量字段通常填 updated_at，增量起点填时间。两者同时填写后，只同步该时间之后变化的数据。",
      "跳过精确 count 可以避免大表 COUNT(*) 很慢，但总行数和剩余时间会更偏估算。",
    ],
  },
  {
    title: "计划、Dry-run 和同步",
    items: [
      "生成计划只预检不写库，用来确认表、字段、行数、策略、游标、分片和风险提示。",
      "Dry-run 会创建运行记录并走完整读取和 SQL 生成流程，但不会向测试库写入数据。",
      "开始同步会正式写入测试库。不同表可以同时启动多个任务；同一张表会自动排队等待。建议对大表先设置 where 或增量条件，再从较小分页和 2 到 4 并发开始试。",
    ],
  },
  {
    title: "运行控制和进度",
    items: [
      "运行页显示状态、百分比、每秒行数、已同步 GB、预计剩余时间和耗时。",
      "暂停会等待当前批次结束后停止，之后可以继续。取消不会直接强杀正在执行的 MySQL SQL，会在下一个批次检查点停住，取消后的运行记录不能继续，已经提交到测试库的数据不会自动回滚。",
      "大表分片会显示每个 shard 的 range 和 last_pk。出现失败后可以根据日志判断是否继续或调整参数重跑。",
    ],
  },
  {
    title: "任务和定时",
    items: [
      "任务名称用于保存当前选表和同步设置。保存后可在任务列表中载入、修改或直接运行。",
      "勾选定时并填写 cron 表达式后，任务会按计划自动执行。例如 0 2 * * * 表示每天 02:00。",
      "定时任务使用保存时的配置。修改界面参数后需要重新保存，定时才会使用新配置。",
    ],
  },
  {
    title: "日志和安全建议",
    items: [
      "同步日志记录在本地运行记录中，运行页和历史详情都可以查看最近日志。",
      "正式同步前建议先 Dry-run。线上账号尽量只读，测试库账号按需授予写入和建表权限。",
      "如果同步很慢，优先检查游标字段、where 或增量条件、分页大小、worker 数以及数据库网络延迟。",
    ],
  },
];

const API_BASE = window.dbSyncDesktop?.apiBase || "";

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("zh-CN");
}

function formatGb(value) {
  return `${Number(value || 0).toFixed(4)} GB`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const total = Math.max(0, Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function syncStrategyLabel(strategy, cursorField = "", workerCount = 1) {
  if (strategy === "cursor") return `大表并发 ${cursorField || "自动游标"} · ${workerCount || 1} 并发`;
  if (strategy === "offset") return "强制 offset";
  return `智能同步${cursorField ? ` · ${cursorField}` : ""}`;
}

function runStatusLabel(status) {
  const labels = {
    queued: "排队中",
    running: "同步中",
    pause_requested: "暂停中",
    paused: "已暂停",
    cancel_requested: "取消中",
    canceled: "已取消",
    success: "已完成",
    failed: "失败",
  };
  return labels[status] || status || "-";
}

function queueStateText(run) {
  if (run?.status !== "queued") return "";
  if (run.queue_state === "waiting_table") {
    return `等待同表任务完成${run.queue_position ? ` · 队列 #${run.queue_position}` : ""}`;
  }
  if (run.queue_state === "waiting_worker") return "等待空闲 worker";
  return "等待调度";
}

function blankConnection() {
  return {
    host: "",
    port: 3306,
    user: "",
    password: "",
    database: "",
    charset: "utf8mb4",
  };
}

function HelpTip({ title, placement = "top" }) {
  return (
    <Tooltip title={title} placement={placement}>
      <span className="help-tip-wrap" onClick={(event) => event.stopPropagation()}>
        <QuestionCircleOutlined className="help-tip" />
      </span>
    </Tooltip>
  );
}

function FieldLabel({ label, help }) {
  return (
    <span className="field-label">
      <span>{label}</span>
      <HelpTip title={help} />
    </span>
  );
}

function ActionHelp({ help }) {
  return <HelpTip title={help} />;
}

function AppShell() {
  const { message } = AntApp.useApp();
  const [status, setStatus] = useState(null);
  const [tables, setTables] = useState([]);
  const [tableQuery, setTableQuery] = useState("");
  const [selectedTables, setSelectedTables] = useState([]);
  const [plan, setPlan] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [runs, setRuns] = useState([]);
  const [runDetails, setRunDetails] = useState({});
  const [selectedRunId, setSelectedRunId] = useState("");
  const [collapsedRunIds, setCollapsedRunIds] = useState(() => new Set());
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);
  const [connectionForm] = Form.useForm();
  const [syncForm] = Form.useForm();
  const [jobForm] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [syncStrategy, setSyncStrategy] = useState(DEFAULT_SYNC_VALUES.sync_strategy);
  const tableShellRef = useRef(null);
  const [leftPaneWidth, setLeftPaneWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem(LEFT_PANE_WIDTH_KEY));
    return Number.isFinite(saved)
      ? Math.min(LEFT_PANE_MAX_WIDTH, Math.max(LEFT_PANE_MIN_WIDTH, saved))
      : 360;
  });
  const [tableScrollY, setTableScrollY] = useState(360);
  const [tableScrollX, setTableScrollX] = useState(320);

  const connectionReady = Boolean(status?.connection_ready);
  const selectedCount = selectedTables.length;

  const filteredTables = useMemo(() => {
    const query = tableQuery.trim().toLowerCase();
    if (!query) return tables;
    return tables.filter((item) => item.name.toLowerCase().includes(query));
  }, [tables, tableQuery]);

  const mergedRuns = useMemo(() => {
    const items = new Map();
    for (const run of runs) {
      items.set(run.id, { ...run, ...(runDetails[run.id] || {}) });
    }
    for (const run of Object.values(runDetails)) {
      items.set(run.id, { ...(items.get(run.id) || {}), ...run });
    }
    return Array.from(items.values()).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }, [runs, runDetails]);

  const activeRunIds = useMemo(
    () => mergedRuns.filter((run) => RUN_STREAM_STATUSES.has(run.status)).map((run) => run.id),
    [mergedRuns],
  );

  const runPanelRuns = useMemo(() => {
    const visible = new Map();
    for (const run of mergedRuns) {
      if (RUN_STREAM_STATUSES.has(run.status)) visible.set(run.id, run);
    }
    const selected = mergedRuns.find((run) => run.id === selectedRunId);
    if (selected) visible.set(selected.id, selected);
    if (!visible.size && mergedRuns[0]) visible.set(mergedRuns[0].id, mergedRuns[0]);
    return Array.from(visible.values()).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }, [mergedRuns, selectedRunId]);

  const pausableRunCount = useMemo(
    () => mergedRuns.filter((run) => RUN_PAUSABLE_STATUSES.has(run.status)).length,
    [mergedRuns],
  );
  const cancelableRunCount = useMemo(
    () => mergedRuns.filter((run) => RUN_STREAM_STATUSES.has(run.status) && RUN_CANCELABLE_STATUSES.has(run.status)).length,
    [mergedRuns],
  );

  function upsertRun(run) {
    setRunDetails((previous) => ({ ...previous, [run.id]: { ...(previous[run.id] || {}), ...run } }));
    setRuns((previous) => {
      const exists = previous.some((item) => item.id === run.id);
      const next = exists ? previous.map((item) => (item.id === run.id ? { ...item, ...run } : item)) : [run, ...previous];
      return next.slice(0, 30);
    });
  }

  function expandRun(runId) {
    setCollapsedRunIds((previous) => {
      if (!previous.has(runId)) return previous;
      const next = new Set(previous);
      next.delete(runId);
      return next;
    });
    setSelectedRunId(runId);
  }

  function collapseRun(run) {
    setSelectedRunId((previous) => (previous === run.id ? "" : previous));
    if (RUN_STREAM_STATUSES.has(run.status)) {
      setCollapsedRunIds((previous) => {
        const next = new Set(previous);
        next.add(run.id);
        return next;
      });
    }
  }

  function isRunExpanded(run) {
    return run.id === selectedRunId || (RUN_STREAM_STATUSES.has(run.status) && !collapsedRunIds.has(run.id));
  }

  async function refreshStatus() {
    const payload = await api("/api/status");
    setStatus(payload);
    connectionForm.setFieldsValue({
      prod: { ...blankConnection(), ...(payload.connections?.prod || {}), password: "" },
      test: { ...blankConnection(), ...(payload.connections?.test || {}), password: "" },
    });
  }

  async function refreshTables() {
    if (!connectionReady) {
      setTables([]);
      return;
    }
    const payload = await api("/api/tables");
    setTables(payload.tables || []);
  }

  async function refreshJobs() {
    const payload = await api("/api/jobs");
    setJobs(payload.jobs || []);
  }

  async function refreshRuns() {
    const payload = await api("/api/runs");
    setRuns(payload.runs || []);
  }

  async function refreshAll() {
    try {
      await refreshStatus();
      await Promise.allSettled([refreshJobs(), refreshRuns()]);
    } catch (error) {
      message.error(error.message);
    }
  }

  useEffect(() => {
    refreshAll();
  }, []);

  useEffect(() => {
    window.localStorage.setItem(LEFT_PANE_WIDTH_KEY, String(leftPaneWidth));
  }, [leftPaneWidth]);

  useEffect(() => {
    if (connectionReady) {
      refreshTables().catch((error) => message.error(error.message));
    }
  }, [connectionReady]);

  useLayoutEffect(() => {
    const shell = tableShellRef.current;
    if (!shell) return undefined;
    const updateHeight = () => {
      const header = shell.querySelector(".ant-table-thead")?.getBoundingClientRect().height || 39;
      setTableScrollY(Math.max(260, Math.floor(shell.clientHeight - header)));
      setTableScrollX(Math.max(280, Math.floor(shell.clientWidth)));
    };
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(shell);
    window.addEventListener("resize", updateHeight);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateHeight);
    };
  }, []);

  useEffect(() => {
    if (!activeRunIds.length) return undefined;
    const streams = activeRunIds.map((runId) => {
      const source = new EventSource(`${API_BASE}/api/runs/${encodeURIComponent(runId)}/events?logs_limit=80`);
      const state = {
        closed: false,
        fallbackTimer: null,
        catchupTimer: null,
        lastEventAt: Date.now(),
      };
      const applyRun = (run) => {
        upsertRun(run);
        if (!RUN_STREAM_STATUSES.has(run.status)) {
          state.closed = true;
          source.close();
          refreshRuns().catch((error) => message.error(error.message));
          return false;
        }
        return true;
      };
      const fetchRunSnapshot = async () => {
        const run = await api(`/api/runs/${runId}?logs_limit=80`);
        return { active: applyRun(run), status: run.status };
      };
      const pollFallback = () => {
        fetchRunSnapshot()
          .then(({ active, status }) => {
            if (active && !state.closed) {
              state.fallbackTimer = window.setTimeout(pollFallback, status === "pause_requested" ? 700 : 1400);
            }
          })
          .catch((error) => message.error(error.message));
      };
      const handleRunEvent = (event) => {
        try {
          state.lastEventAt = Date.now();
          applyRun(JSON.parse(event.data));
        } catch (error) {
          message.error(error.message);
        }
      };
      source.addEventListener("run", handleRunEvent);
      source.onmessage = handleRunEvent;
      source.onerror = () => {
        if (state.closed) return;
        source.close();
        pollFallback();
      };
      state.catchupTimer = window.setInterval(() => {
        if (state.closed || Date.now() - state.lastEventAt < 5000) return;
        fetchRunSnapshot()
          .then(() => {
            state.lastEventAt = Date.now();
          })
          .catch(() => {
            if (!state.closed) source.onerror();
          });
      }, 5000);
      return { source, state, handleRunEvent };
    });
    return () => {
      for (const { source, state, handleRunEvent } of streams) {
        state.closed = true;
        window.clearTimeout(state.fallbackTimer);
        window.clearInterval(state.catchupTimer);
        source.removeEventListener("run", handleRunEvent);
        source.close();
      }
    };
  }, [activeRunIds.join(",")]);

  function changeSyncStrategy(value) {
    setSyncStrategy(value);
    const currentBatchSize = Number(syncForm.getFieldValue("batch_size") || 0);
    syncForm.setFieldsValue({
      sync_strategy: value,
      ...(value !== "offset" && currentBatchSize <= DEFAULT_SYNC_VALUES.batch_size
        ? { batch_size: BIG_TABLE_BATCH_SIZE }
        : {}),
    });
    setPlan(null);
  }

  const syncValues = () => {
    const values = syncForm.getFieldsValue(true);
    return {
      ...DEFAULT_SYNC_VALUES,
      ...values,
      sync_strategy: syncStrategy,
      tables: selectedTables,
      name: jobForm.getFieldValue("name") || null,
    };
  };

  async function buildPlan() {
    const payload = syncValues();
    if (!payload.tables.length) {
      message.warning("请选择表");
      return;
    }
    setBusy(true);
    try {
      const result = await api("/api/plan", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setPlan(result);
      message.success("计划已生成");
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function startRun(dryRun = false) {
    const payload = { ...syncValues(), dry_run: dryRun };
    if (!payload.tables.length) {
      message.warning("请选择表");
      return;
    }
    setBusy(true);
    try {
      const run = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      upsertRun(run);
      expandRun(run.id);
      await refreshRuns();
      message.success(dryRun ? "dry-run 已加入队列" : "同步已启动");
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveJob() {
    const payload = { ...syncValues(), ...jobForm.getFieldsValue() };
    if (!payload.name) {
      message.warning("请输入任务名称");
      return;
    }
    if (!payload.tables.length) {
      message.warning("请选择表");
      return;
    }
    setBusy(true);
    try {
      await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await refreshJobs();
      message.success("任务已保存");
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function runJob(job) {
    setBusy(true);
    try {
      const run = await api(`/api/jobs/${job.id}/run`, { method: "POST" });
      upsertRun(run);
      expandRun(run.id);
      await refreshRuns();
      message.success("任务已启动");
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy(false);
    }
  }

  function loadJob(job) {
    const nextStrategy = job.sync_strategy || DEFAULT_SYNC_VALUES.sync_strategy;
    setSelectedTables(job.tables || []);
    setSyncStrategy(nextStrategy);
    syncForm.setFieldsValue({
      mode: job.mode,
      where_clause: job.where_clause,
      batch_size: job.batch_size,
      create_missing_tables: job.create_missing_tables,
      sync_strategy: nextStrategy,
      cursor_field: job.cursor_field,
      incremental_field: job.incremental_field,
      incremental_since: job.incremental_since,
      skip_exact_count: job.skip_exact_count,
      shard_count: job.shard_count,
      worker_count: job.worker_count,
    });
    jobForm.setFieldsValue({
      name: job.name,
      schedule_enabled: job.schedule_enabled,
      cron_expr: job.cron_expr,
    });
    message.success("任务已载入");
  }

  async function openRun(run) {
    if (isRunExpanded(run)) {
      collapseRun(run);
      return;
    }
    try {
      const detail = await api(`/api/runs/${run.id}?logs_limit=120`);
      upsertRun(detail);
      expandRun(detail.id);
    } catch (error) {
      message.error(error.message);
    }
  }

  async function resumeRun(runId) {
    try {
      const run = await api(`/api/runs/${runId}/resume`, { method: "POST" });
      upsertRun(run);
      expandRun(run.id);
      await refreshRuns();
      message.success("已继续");
    } catch (error) {
      message.error(error.message);
    }
  }

  async function pauseRun(runId) {
    try {
      const run = await api(`/api/runs/${runId}/pause`, { method: "POST" });
      upsertRun(run);
      expandRun(run.id);
      message.success("已请求暂停，当前批次完成后会停住");
    } catch (error) {
      message.error(error.message);
    }
  }

  async function pauseActiveRuns() {
    const targets = mergedRuns.filter((run) => RUN_PAUSABLE_STATUSES.has(run.status));
    if (!targets.length) {
      message.info("没有可暂停的活动任务");
      return;
    }
    const results = await Promise.allSettled(
      targets.map((run) => api(`/api/runs/${run.id}/pause`, { method: "POST" })),
    );
    let successCount = 0;
    for (const result of results) {
      if (result.status === "fulfilled") {
        successCount += 1;
        upsertRun(result.value);
      } else {
        message.error(result.reason?.message || "暂停失败");
      }
    }
    await refreshRuns();
    if (successCount) message.success(`已请求暂停 ${successCount} 个任务`);
  }

  function startResizeLeftPane(event) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = leftPaneWidth;
    const onMove = (moveEvent) => {
      const maxWidth = Math.min(LEFT_PANE_MAX_WIDTH, Math.floor(window.innerWidth * 0.48));
      const nextWidth = Math.min(maxWidth, Math.max(LEFT_PANE_MIN_WIDTH, startWidth + moveEvent.clientX - startX));
      setLeftPaneWidth(nextWidth);
    };
    const onUp = () => {
      document.body.classList.remove("is-resizing-left-pane");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    document.body.classList.add("is-resizing-left-pane");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  async function cancelRun(runId) {
    try {
      const run = await api(`/api/runs/${runId}/cancel`, { method: "POST" });
      upsertRun(run);
      expandRun(run.id);
      message.warning(run.status === "canceled" ? "已取消" : "已请求取消，当前批次完成后会停住");
    } catch (error) {
      message.error(error.message);
    }
  }

  async function cancelActiveRuns() {
    const targets = mergedRuns.filter((run) => RUN_STREAM_STATUSES.has(run.status) && RUN_CANCELABLE_STATUSES.has(run.status));
    if (!targets.length) {
      message.info("没有可取消的活动任务");
      return;
    }
    const results = await Promise.allSettled(
      targets.map((run) => api(`/api/runs/${run.id}/cancel`, { method: "POST" })),
    );
    let successCount = 0;
    for (const result of results) {
      if (result.status === "fulfilled") {
        successCount += 1;
        upsertRun(result.value);
      } else {
        message.error(result.reason?.message || "取消失败");
      }
    }
    await refreshRuns();
    if (successCount) message.warning(`已请求取消 ${successCount} 个任务`);
  }

  async function saveConnections(login = false) {
    const values = connectionForm.getFieldsValue();
    setBusy(true);
    try {
      await api(login ? "/api/connections/login" : "/api/connections", {
        method: "POST",
        body: JSON.stringify(values),
      });
      connectionForm.setFieldsValue({
        prod: { ...values.prod, password: "" },
        test: { ...values.test, password: "" },
      });
      await refreshStatus();
      if (login) await refreshTables();
      message.success(login ? "连接成功" : "连接已保存");
      if (login) setConnectionOpen(false);
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function testConnection(env) {
    const connection = connectionForm.getFieldValue(env);
    try {
      const result = await api("/api/connections/test", {
        method: "POST",
        body: JSON.stringify({ env, connection }),
      });
      message.success(`${env === "prod" ? "产品库" : "测试库"}连接成功：${result.result.database}`);
    } catch (error) {
      message.error(error.message);
    }
  }

  return (
    <Layout className="desktop-root">
      <Header className="app-header">
        <div className="window-drag-space" />
        <div className="title-block">
          <img className="app-logo" src={APP_LOGO_URL} alt="" draggable={false} />
          <div className="title-copy">
            <Title level={4}>{APP_NAME}</Title>
            <Text type="secondary">
              {status?.config?.prod?.database || "prod"} @ {status?.config?.prod?.host || "-"} →{" "}
              {status?.config?.test?.database || "test"} @ {status?.config?.test?.host || "-"}
            </Text>
          </div>
        </div>
        <Space>
          <Badge status={connectionReady ? "success" : "default"} text={connectionReady ? "已连接" : "未连接"} />
          <Tag color="geekblue">MySQL</Tag>
          <Button icon={<BookOutlined />} onClick={() => setDocsOpen(true)}>
            使用文档
          </Button>
          <Button icon={<SettingOutlined />} onClick={() => setConnectionOpen(true)}>
            连接
          </Button>
          <Button icon={<ReloadOutlined />} onClick={refreshAll}>
            刷新
          </Button>
        </Space>
      </Header>

      <Layout className="app-body">
        <Sider width={leftPaneWidth} className="left-pane">
          <SectionHeader
            icon={<DatabaseOutlined />}
            title="线上表"
            extra={<Tag color="blue">已选 {selectedCount}</Tag>}
            help="这里展示产品库表。搜索只过滤显示，已勾选的表会一直保留，点击开始同步时会同步所有已选表。"
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            suffix={<HelpTip title={HELP_TEXT.tableSearch} placement="right" />}
            placeholder="搜索表名"
            value={tableQuery}
            onChange={(event) => setTableQuery(event.target.value)}
            className="pane-search"
          />
          <div className="table-actions-row">
            <Space.Compact block className="table-actions">
              <Button onClick={() => setSelectedTables(filteredTables.map((item) => item.name))}>选择当前</Button>
              <Button onClick={() => setSelectedTables([])}>清空</Button>
              <Button icon={<ReloadOutlined />} onClick={refreshTables} />
            </Space.Compact>
            <HelpTip
              placement="right"
              title={`${HELP_TEXT.selectVisible} ${HELP_TEXT.clearTables} ${HELP_TEXT.refreshTables}`}
            />
          </div>
          <div className="table-list-shell" ref={tableShellRef}>
            <Table
              size="small"
              rowKey="name"
              virtual
              pagination={false}
              scroll={{ y: tableScrollY, x: tableScrollX }}
              dataSource={filteredTables}
              rowSelection={{
                columnWidth: 28,
                selectedRowKeys: selectedTables,
                preserveSelectedRowKeys: true,
                onChange: (keys) => setSelectedTables(keys),
              }}
              columns={[
                {
                  title: "表名",
                  dataIndex: "name",
                  ellipsis: true,
                  render: (value) => (
                    <Tooltip title={value}>
                      <span className="table-name-text">{value}</span>
                    </Tooltip>
                  ),
                },
                {
                  title: "行数",
                  dataIndex: "estimated_rows",
                  width: 68,
                  align: "right",
                  render: formatNumber,
                },
              ]}
              locale={{ emptyText: connectionReady ? <Empty description="没有表" /> : <Empty description="请先连接数据库" /> }}
            />
          </div>
          <div
            className="left-pane-resizer"
            role="separator"
            aria-orientation="vertical"
            aria-label="调整线上表列表宽度"
            onMouseDown={startResizeLeftPane}
          />
        </Sider>

        <Content className="center-pane">
          <div className="toolbar-strip">
            <div className="strategy-control">
              <Segmented
                value={syncStrategy}
                options={[
                  { label: "智能同步", value: "auto" },
                  { label: "强制 offset", value: "offset" },
                  { label: "大表并发", value: "cursor" },
                ]}
                onChange={changeSyncStrategy}
              />
              <HelpTip title={HELP_TEXT.strategy} />
            </div>
            <Space className="primary-actions">
              <span className="action-with-help">
                <Button title="生成计划" icon={<CloudSyncOutlined />} onClick={buildPlan} loading={busy}>
                  生成计划
                </Button>
                <ActionHelp help={HELP_TEXT.plan} />
              </span>
              <span className="action-with-help">
                <Button title="Dry-run" icon={<FieldTimeOutlined />} onClick={() => startRun(true)} loading={busy}>
                  Dry-run
                </Button>
                <ActionHelp help={HELP_TEXT.dryRun} />
              </span>
              <span className="action-with-help">
                <Button title="开始同步" type="primary" icon={<PlayCircleOutlined />} onClick={() => startRun(false)} loading={busy}>
                  开始同步
                </Button>
                <ActionHelp help={HELP_TEXT.start} />
              </span>
            </Space>
          </div>

          <div className="center-grid">
            <section className="panel settings-panel">
              <SectionHeader icon={<ThunderboltOutlined />} title="同步设置" help="配置读取、过滤、写入和大表并发参数。这里的设置会用于生成计划、Dry-run、正式同步和保存任务。" />
              <div className="feature-hints">
                <Text type="secondary">生成计划只预检不写库；Dry-run 会创建运行记录但不写测试库；暂停可继续，取消不可继续。</Text>
              </div>
              <Form
                form={syncForm}
                layout="vertical"
                initialValues={DEFAULT_SYNC_VALUES}
              >
                <Form.Item name="sync_strategy" hidden>
                  <Input />
                </Form.Item>
                <div className="sync-form-grid">
                  <Form.Item label={<FieldLabel label="写入模式" help={HELP_TEXT.mode} />} name="mode">
                    <Select
                      options={[
                        { label: "replace 覆盖", value: "replace" },
                        { label: "upsert 插入或更新", value: "upsert" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="分页大小" help={HELP_TEXT.batchSize} />} name="batch_size">
                    <InputNumber min={1} />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="游标字段" help={HELP_TEXT.cursor} />} name="cursor_field">
                    <Input placeholder={syncStrategy === "offset" ? "强制 offset 时不使用" : "留空自动选择，支持 updated_at,id"} disabled={syncStrategy === "offset"} />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="增量字段" help={HELP_TEXT.incrementalField} />} name="incremental_field">
                    <Input placeholder="updated_at" />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="增量起点" help={HELP_TEXT.incrementalSince} />} name="incremental_since">
                    <Input placeholder="2026-07-01 00:00:00" />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="分片数" help={HELP_TEXT.shardCount} />} name="shard_count">
                    <InputNumber min={1} max={64} disabled={syncStrategy === "offset"} />
                  </Form.Item>
                  <Form.Item label={<FieldLabel label="并发数" help={HELP_TEXT.workerCount} />} name="worker_count">
                    <InputNumber min={1} max={8} disabled={syncStrategy === "offset"} />
                  </Form.Item>
                  <Form.Item label=" " name="create_missing_tables" valuePropName="checked">
                    <Checkbox>
                      <span className="checkbox-label">缺表自动建表 <HelpTip title={HELP_TEXT.createMissing} /></span>
                    </Checkbox>
                  </Form.Item>
                  <Form.Item label=" " name="skip_exact_count" valuePropName="checked">
                    <Checkbox>
                      <span className="checkbox-label">跳过精确 count <HelpTip title={HELP_TEXT.skipCount} /></span>
                    </Checkbox>
                  </Form.Item>
                </div>
                <Form.Item label={<FieldLabel label="where 条件" help={HELP_TEXT.where} />} name="where_clause">
                  <Input.TextArea rows={2} placeholder="例如 created_at >= '2026-07-01'" />
                </Form.Item>
              </Form>
            </section>

            <section className="panel plan-panel">
              <SectionHeader
                icon={<BranchesOutlined />}
                title="同步计划"
                extra={plan ? `${formatNumber(plan.total_rows)} 行 / ${plan.table_count} 表` : "尚未生成"}
                help={HELP_TEXT.planPanel}
              />
              {plan?.warnings?.length ? <Alert type="warning" showIcon message={plan.warnings.join("；")} /> : null}
              <div className="plan-table-shell">
                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  dataSource={plan?.tables || []}
                  columns={[
                    { title: "表", dataIndex: "name", ellipsis: true },
                    { title: "动作", dataIndex: "action", ellipsis: true },
                    {
                      title: "读取",
                      dataIndex: "pagination_strategy",
                      width: 88,
                      render: (value) => (value === "cursor" ? "keyset" : value || "-"),
                    },
                    {
                      title: "行数",
                      dataIndex: "row_count",
                      width: 110,
                      align: "right",
                      render: (value, row) => `${formatNumber(value)}${row.estimated ? " 估算" : ""}`,
                    },
                    {
                      title: "字段",
                      dataIndex: "columns",
                      width: 82,
                      align: "right",
                      render: (value, row) => `${value?.length || 0}/${row.source_columns?.length || value?.length || 0}`,
                    },
                    { title: "游标", dataIndex: "cursor_field", width: 120, render: (value) => value || "-" },
                    { title: "分片", dataIndex: "shard_count", width: 70, align: "right" },
                  ]}
                  locale={{ emptyText: <Empty description="生成计划后查看" /> }}
                />
              </div>
            </section>
          </div>
        </Content>

        <Sider width={390} className="right-pane">
          <Tabs
            className="right-tabs"
            items={[
              {
                key: "run",
                label: (
                  <span className="tab-label">
                    运行
                    <HelpTip title={HELP_TEXT.runPanel} />
                  </span>
                ),
                children: (
                  <RunsPanel
                    runs={runPanelRuns}
                    isRunExpanded={isRunExpanded}
                    pausableRunCount={pausableRunCount}
                    cancelableRunCount={cancelableRunCount}
                    onOpen={openRun}
                    onResume={resumeRun}
                    onPause={pauseRun}
                    onCancel={cancelRun}
                    onPauseAll={pauseActiveRuns}
                    onCancelAll={cancelActiveRuns}
                  />
                ),
              },
              {
                key: "jobs",
                label: (
                  <span className="tab-label">
                    任务
                    <HelpTip title="保存常用同步配置，也可以开启 cron 定时自动执行。" />
                  </span>
                ),
                children: (
                  <JobsPanel
                    jobs={jobs}
                    jobForm={jobForm}
                    onSave={saveJob}
                    onRun={runJob}
                    onLoad={loadJob}
                    busy={busy}
                  />
                ),
              },
              {
                key: "history",
                label: (
                  <span className="tab-label">
                    历史
                    <HelpTip title={HELP_TEXT.history} />
                  </span>
                ),
                children: <HistoryPanel runs={runs} onOpen={openRun} />,
              },
            ]}
          />
        </Sider>
      </Layout>

      <ConnectionDrawer
        open={connectionOpen}
        form={connectionForm}
        status={status}
        busy={busy}
        onClose={() => setConnectionOpen(false)}
        onSave={() => saveConnections(false)}
        onLogin={() => saveConnections(true)}
        onTest={testConnection}
      />
      <DocsDrawer open={docsOpen} onClose={() => setDocsOpen(false)} />
    </Layout>
  );
}

function SectionHeader({ icon, title, extra, help }) {
  return (
    <div className="section-header">
      <Space>
        {icon}
        <Text strong>{title}</Text>
        {help ? <HelpTip title={help} /> : null}
      </Space>
      {extra ? <Text type="secondary">{extra}</Text> : null}
    </div>
  );
}

function runPercent(run) {
  if (run?.total_rows) {
    return Math.min(100, Math.round((run.processed_rows / run.total_rows) * 100));
  }
  return run?.status === "success" ? 100 : 0;
}

function RunsPanel({
  runs,
  isRunExpanded,
  pausableRunCount,
  cancelableRunCount,
  onOpen,
  onResume,
  onPause,
  onCancel,
  onPauseAll,
  onCancelAll,
}) {
  if (!runs.length) {
    return <Empty className="pane-empty" description="尚无运行" />;
  }
  return (
    <div className="runs-panel">
      <div className="runs-toolbar">
        <span className="runs-toolbar-title">
          <Text type="secondary">活动 {pausableRunCount || cancelableRunCount || 0}</Text>
          <HelpTip title={HELP_TEXT.runBatch} />
        </span>
        <Space size={6}>
          <Button size="small" icon={<PauseCircleOutlined />} disabled={!pausableRunCount} onClick={onPauseAll}>
            暂停全部
          </Button>
          <Popconfirm
            title="取消所有活动任务？"
            description="已经提交的批次不会自动回滚。"
            okText="取消任务"
            cancelText="再想想"
            okButtonProps={{ danger: true }}
            onConfirm={onCancelAll}
          >
            <Button size="small" danger icon={<StopOutlined />} disabled={!cancelableRunCount}>
              取消全部
            </Button>
          </Popconfirm>
        </Space>
      </div>
      <Space direction="vertical" size={10} className="full-width">
        {runs.map((run) => (
          <RunCard
            key={run.id}
            run={run}
            expanded={isRunExpanded(run)}
            onOpen={onOpen}
            onResume={onResume}
            onPause={onPause}
            onCancel={onCancel}
          />
        ))}
      </Space>
    </div>
  );
}

function RunCard({ run, expanded, onOpen, onResume, onPause, onCancel }) {
  const statusColor =
    run.status === "success"
      ? "success"
      : run.status === "failed"
        ? "error"
        : run.status === "paused" || run.status === "canceled"
          ? "warning"
          : "processing";
  const canPause = RUN_PAUSABLE_STATUSES.has(run.status);
  const canCancel = RUN_CANCELABLE_STATUSES.has(run.status);
  const canResume = RUN_RESUMABLE_STATUSES.has(run.status);
  const shards = run.shards_state || [];
  const percent = runPercent(run);
  const queueText = queueStateText(run);
  return (
    <div className={`run-card ${expanded ? "run-card-expanded" : ""}`}>
      <Space direction="vertical" size={10} className="full-width">
        <button className="run-card-header" onClick={() => onOpen(run)}>
          <span className="run-title">
            {expanded ? <DownOutlined className="run-expand-icon" /> : <RightOutlined className="run-expand-icon" />}
            <Badge status={statusColor} />
            <Text strong ellipsis>{run.name}</Text>
          </span>
          <span className="run-card-tags">
            <Tag>{runStatusLabel(run.status)}</Tag>
            <Tag>{syncStrategyLabel(run.sync_strategy, run.cursor_field, run.worker_count)}</Tag>
          </span>
        </button>
        {queueText ? <Text className="run-queue-note" type="secondary">{queueText}</Text> : null}
        <Progress percent={percent} status={run.status === "failed" ? "exception" : undefined} />
        <div className="metric-grid">
          <Statistic title={<FieldLabel label="速度" help="当前运行平均吞吐，单位是每秒处理行数。" />} value={run.rows_per_second || 0} precision={2} suffix="行/秒" />
          <Statistic title={<FieldLabel label="已同步" help="根据已处理数据估算出的数据量，便于观察大表同步进度。" />} value={Number(run.synced_gb || 0)} precision={4} suffix="GB" />
          <Statistic title={<FieldLabel label="剩余" help="根据当前速度和总量估算的剩余时间。跳过精确 count 时会更偏估算。" />} value={formatDuration(run.eta_seconds)} />
          <Statistic title={<FieldLabel label="耗时" help="本次运行从启动到现在的累计耗时。" />} value={formatDuration(run.elapsed_seconds || 0)} />
        </div>
        <div className="run-actions">
          {canPause ? (
            <span className="action-with-help">
              <Button
                icon={<PauseCircleOutlined />}
                onClick={() => onPause(run.id)}
                loading={run.status === "pause_requested"}
                disabled={run.status === "pause_requested" || run.status === "cancel_requested"}
              >
                {run.status === "pause_requested" ? "暂停中" : "暂停"}
              </Button>
              <ActionHelp help={HELP_TEXT.pause} />
            </span>
          ) : null}
          {canCancel ? (
            <span className="action-with-help">
              <Popconfirm
                title="取消这次同步？"
                description="取消后不能继续这个运行记录。"
                okText="取消同步"
                cancelText="再想想"
                okButtonProps={{ danger: true }}
                onConfirm={() => onCancel(run.id)}
              >
                <Button danger icon={<StopOutlined />} loading={run.status === "cancel_requested"} disabled={run.status === "cancel_requested"}>
                  {run.status === "cancel_requested" ? "取消中" : "取消"}
                </Button>
              </Popconfirm>
              <ActionHelp help={HELP_TEXT.cancel} />
            </span>
          ) : null}
          {canResume ? (
            <span className="action-with-help">
              <Button type={run.status === "paused" ? "primary" : "default"} danger={run.status === "failed"} onClick={() => onResume(run.id)}>
                继续
              </Button>
              <ActionHelp help={HELP_TEXT.resume} />
            </span>
          ) : null}
        </div>
        {expanded ? (
          <>
            <List
              size="small"
              dataSource={run.tables_state || []}
              locale={{ emptyText: <Empty description="点击任务可加载详情" /> }}
              renderItem={(table) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space>
                        <Text>{table.table_name}</Text>
                        <Tag>{runStatusLabel(table.status)}</Tag>
                      </Space>
                    }
                    description={`${formatNumber(table.processed_rows)} / ${formatNumber(table.total_rows)} 行 · ${
                      table.cursor_field ? `last_pk ${table.last_pk || "-"}` : `offset ${formatNumber(table.offset_value)}`
                    }`}
                  />
                </List.Item>
              )}
            />
            {shards.length ? (
              <div className="shard-panel">
                <Text strong>分片进度</Text>
                <List
                  size="small"
                  dataSource={shards}
                  renderItem={(shard) => (
                    <List.Item>
                      <List.Item.Meta
                        title={
                          <Space>
                            <Text>#{shard.shard_index}</Text>
                            <Tag>{runStatusLabel(shard.status)}</Tag>
                          </Space>
                        }
                        description={`${formatNumber(shard.processed_rows)} 行 · range ${
                          shard.start_pk || "-"
                        }..${shard.end_pk || "-"} · last_pk ${shard.last_pk || "-"}`}
                      />
                    </List.Item>
                  )}
                />
              </div>
            ) : null}
            <pre className="log-box">
              {(run.logs || [])
                .map((item) => `${item.created_at} [${item.level.toUpperCase()}] ${item.message}`)
                .join("\n")}
            </pre>
          </>
        ) : null}
      </Space>
    </div>
  );
}

function JobsPanel({ jobs, jobForm, onSave, onRun, onLoad, busy }) {
  return (
    <div className="jobs-panel">
      <Form form={jobForm} layout="vertical" initialValues={{ schedule_enabled: false, cron_expr: "" }}>
        <Form.Item label={<FieldLabel label="任务名称" help={HELP_TEXT.jobName} />} name="name">
          <Input placeholder="例如 库存批次同步" />
        </Form.Item>
        <div className="job-save-grid">
          <Form.Item name="schedule_enabled" valuePropName="checked">
            <Checkbox>
              <span className="checkbox-label">定时 <HelpTip title={HELP_TEXT.schedule} /></span>
            </Checkbox>
          </Form.Item>
          <Form.Item name="cron_expr">
            <Input placeholder="0 2 * * *" suffix={<HelpTip title={HELP_TEXT.cron} placement="left" />} />
          </Form.Item>
          <span className="action-with-help">
            <Button icon={<SaveOutlined />} onClick={onSave} loading={busy}>
              保存
            </Button>
            <ActionHelp help={HELP_TEXT.saveJob} />
          </span>
        </div>
      </Form>
      <List
        className="side-list"
        dataSource={jobs}
        locale={{ emptyText: <Empty description="暂无任务" /> }}
        renderItem={(job) => (
          <List.Item
            actions={[
              <span key="load" className="action-with-help compact-help-action">
                <Button size="small" onClick={() => onLoad(job)}>载入</Button>
                <ActionHelp help={HELP_TEXT.jobLoad} />
              </span>,
              <span key="run" className="action-with-help compact-help-action">
                <Button size="small" type="primary" onClick={() => onRun(job)}>运行</Button>
                <ActionHelp help={HELP_TEXT.jobRun} />
              </span>,
            ]}
          >
            <List.Item.Meta
              title={job.name}
              description={`${job.tables.join(", ")} · ${syncStrategyLabel(job.sync_strategy, job.cursor_field, job.worker_count)}`}
            />
          </List.Item>
        )}
      />
    </div>
  );
}

function HistoryPanel({ runs, onOpen }) {
  return (
    <List
      className="side-list history-list"
      dataSource={runs}
      locale={{ emptyText: <Empty description="暂无历史" /> }}
      renderItem={(run) => (
        <List.Item onClick={() => onOpen(run)} className="clickable-row">
          <List.Item.Meta
            title={
              <Space>
                <Text>{run.name}</Text>
                <Tag>{runStatusLabel(run.status)}</Tag>
              </Space>
            }
            description={`${run.created_at} · ${run.tables.join(", ")}`}
          />
        </List.Item>
      )}
    />
  );
}

function ConnectionDrawer({ open, form, status, busy, onClose, onSave, onLogin, onTest }) {
  return (
    <Drawer
      title="数据库连接"
      width={620}
      open={open}
      onClose={onClose}
      extra={
        <Space>
          <span className="action-with-help">
            <Button onClick={onSave} loading={busy}>保存</Button>
            <ActionHelp help="保存当前连接配置。密码留空时会沿用已经保存的密码。" />
          </span>
          <span className="action-with-help">
            <Button type="primary" onClick={onLogin} loading={busy}>测试并登录</Button>
            <ActionHelp help="同时测试产品库和测试库连接，成功后关闭窗口并刷新线上表列表。" />
          </span>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        message="当前 provider 支持 MySQL；产品库建议使用只读账号。"
        className="drawer-alert"
      />
      <Form form={form} layout="vertical">
        <ConnectionBlock title="产品库" name="prod" passwordSet={status?.connections?.prod?.password_set} onTest={onTest} />
        <ConnectionBlock title="测试库" name="test" passwordSet={status?.connections?.test?.password_set} onTest={onTest} />
      </Form>
    </Drawer>
  );
}

function ConnectionBlock({ title, name, passwordSet, onTest }) {
  const isProd = name === "prod";
  return (
    <section className="connection-block">
      <SectionHeader
        icon={<ApiOutlined />}
        title={title}
        extra={passwordSet ? <Tag color="green">已保存密码</Tag> : null}
        help={isProd ? HELP_TEXT.prodConnection : HELP_TEXT.testConnection}
      />
      <div className="connection-form-grid">
        <Form.Item label={<FieldLabel label="Host" help="数据库地址，例如 RDS 域名或内网 IP。注意不要多填空格或错误后缀。" />} name={[name, "host"]}>
          <Input />
        </Form.Item>
        <Form.Item label={<FieldLabel label="Port" help="MySQL 默认端口是 3306。如云数据库使用自定义端口，请填实际端口。" />} name={[name, "port"]}>
          <InputNumber min={1} />
        </Form.Item>
        <Form.Item label={<FieldLabel label="User" help={isProd ? "产品库建议使用只读账号。" : "测试库账号需要写入权限。"} />} name={[name, "user"]}>
          <Input />
        </Form.Item>
        <Form.Item label={<FieldLabel label="Password" help="密码会保存在本地。已有密码时留空表示沿用已保存密码。" />} name={[name, "password"]}>
          <Input.Password placeholder={passwordSet ? "留空沿用已保存密码" : ""} />
        </Form.Item>
        <Form.Item label={<FieldLabel label="Database" help="要同步的数据库名。产品库和测试库可以不同名。" />} name={[name, "database"]}>
          <Input />
        </Form.Item>
        <Form.Item label={<FieldLabel label="Charset" help="连接字符集，通常使用 utf8mb4。" />} name={[name, "charset"]}>
          <Input />
        </Form.Item>
      </div>
      <span className="action-with-help">
        <Button icon={<CheckCircleOutlined />} onClick={() => onTest(name)}>
          测试{name === "prod" ? "产品库" : "测试库"}
        </Button>
        <ActionHelp help={HELP_TEXT.testButton} />
      </span>
    </section>
  );
}

function DocsDrawer({ open, onClose }) {
  return (
    <Drawer
      title={
        <Space>
          <BookOutlined />
          <span>使用文档</span>
        </Space>
      }
      width={760}
      open={open}
      onClose={onClose}
      className="docs-drawer"
    >
      <Alert
        type="info"
        showIcon
        message="同步犬用于把产品库的指定表数据同步到测试库。正式同步前，建议先生成计划，再执行 Dry-run。"
        className="drawer-alert"
      />
      <div className="docs-content">
        {DOC_SECTIONS.map((section) => (
          <section className="doc-section" key={section.title}>
            <Title level={5}>{section.title}</Title>
            <ol>
              {section.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ol>
          </section>
        ))}
      </div>
    </Drawer>
  );
}

function Root() {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#1677ff",
          borderRadius: 8,
          fontSize: 13,
        },
        components: {
          Layout: {
            bodyBg: "#eef2f6",
            headerBg: "#f8fafc",
            siderBg: "#f8fafc",
          },
        },
      }}
    >
      <AntApp>
        <AppShell />
      </AntApp>
    </ConfigProvider>
  );
}

createRoot(document.getElementById("root")).render(<Root />);
