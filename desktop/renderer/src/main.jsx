import React, { useEffect, useMemo, useState } from "react";
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
  BranchesOutlined,
  CheckCircleOutlined,
  CloudSyncOutlined,
  DatabaseOutlined,
  FieldTimeOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import "antd/dist/reset.css";
import "./styles.css";

const { Header, Sider, Content } = Layout;
const { Text, Title } = Typography;
const APP_NAME = "同步犬";
const BIG_TABLE_BATCH_SIZE = 5000;
const DEFAULT_SYNC_VALUES = {
  mode: "replace",
  batch_size: 1000,
  create_missing_tables: false,
  sync_strategy: "offset",
  cursor_field: "",
  incremental_field: "",
  incremental_since: "",
  skip_exact_count: false,
  shard_count: 2,
  worker_count: 2,
  where_clause: "",
};

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

function AppShell() {
  const { message } = AntApp.useApp();
  const [status, setStatus] = useState(null);
  const [tables, setTables] = useState([]);
  const [tableQuery, setTableQuery] = useState("");
  const [selectedTables, setSelectedTables] = useState([]);
  const [plan, setPlan] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [runs, setRuns] = useState([]);
  const [currentRun, setCurrentRun] = useState(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [connectionForm] = Form.useForm();
  const [syncForm] = Form.useForm();
  const [jobForm] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [syncStrategy, setSyncStrategy] = useState(DEFAULT_SYNC_VALUES.sync_strategy);

  const connectionReady = Boolean(status?.connection_ready);
  const selectedCount = selectedTables.length;

  const filteredTables = useMemo(() => {
    const query = tableQuery.trim().toLowerCase();
    if (!query) return tables;
    return tables.filter((item) => item.name.toLowerCase().includes(query));
  }, [tables, tableQuery]);

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
    if (connectionReady) {
      refreshTables().catch((error) => message.error(error.message));
    }
  }, [connectionReady]);

  useEffect(() => {
    if (!currentRun || !["queued", "running"].includes(currentRun.status)) return;
    const timer = setTimeout(async () => {
      try {
        const run = await api(`/api/runs/${currentRun.id}`);
        setCurrentRun(run);
        if (!["queued", "running"].includes(run.status)) {
          refreshRuns();
        }
      } catch (error) {
        message.error(error.message);
      }
    }, 1200);
    return () => clearTimeout(timer);
  }, [currentRun]);

  function changeSyncStrategy(value) {
    setSyncStrategy(value);
    const currentBatchSize = Number(syncForm.getFieldValue("batch_size") || 0);
    syncForm.setFieldsValue({
      sync_strategy: value,
      ...(value === "cursor" && currentBatchSize <= DEFAULT_SYNC_VALUES.batch_size
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
      setCurrentRun(run);
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
      setCurrentRun(run);
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
    try {
      const detail = await api(`/api/runs/${run.id}`);
      setCurrentRun(detail);
    } catch (error) {
      message.error(error.message);
    }
  }

  async function resumeRun() {
    if (!currentRun) return;
    try {
      const run = await api(`/api/runs/${currentRun.id}/resume`, { method: "POST" });
      setCurrentRun(run);
      message.success("已继续");
    } catch (error) {
      message.error(error.message);
    }
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

  const runPercent = currentRun?.total_rows
    ? Math.min(100, Math.round((currentRun.processed_rows / currentRun.total_rows) * 100))
    : currentRun?.status === "success"
      ? 100
      : 0;

  return (
    <Layout className="desktop-root">
      <Header className="app-header">
        <div className="window-drag-space" />
        <div className="title-block">
          <Title level={4}>{APP_NAME}</Title>
          <Text type="secondary">
            {status?.config?.prod?.database || "prod"} @ {status?.config?.prod?.host || "-"} →{" "}
            {status?.config?.test?.database || "test"} @ {status?.config?.test?.host || "-"}
          </Text>
        </div>
        <Space>
          <Badge status={connectionReady ? "success" : "default"} text={connectionReady ? "已连接" : "未连接"} />
          <Tag color="geekblue">MySQL</Tag>
          <Button icon={<SettingOutlined />} onClick={() => setConnectionOpen(true)}>
            连接
          </Button>
          <Button icon={<ReloadOutlined />} onClick={refreshAll}>
            刷新
          </Button>
        </Space>
      </Header>

      <Layout className="app-body">
        <Sider width={360} className="left-pane">
          <SectionHeader
            icon={<DatabaseOutlined />}
            title="线上表"
            extra={<Tag color="blue">已选 {selectedCount}</Tag>}
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索表名"
            value={tableQuery}
            onChange={(event) => setTableQuery(event.target.value)}
            className="pane-search"
          />
          <Space.Compact block className="table-actions">
            <Button onClick={() => setSelectedTables(filteredTables.map((item) => item.name))}>选择当前</Button>
            <Button onClick={() => setSelectedTables([])}>清空</Button>
            <Button icon={<ReloadOutlined />} onClick={refreshTables} />
          </Space.Compact>
          <div className="table-list-shell">
            <Table
              size="small"
              rowKey="name"
              pagination={false}
              dataSource={filteredTables}
              rowSelection={{
                selectedRowKeys: selectedTables,
                onChange: (keys) => setSelectedTables(keys),
              }}
              columns={[
                {
                  title: "表名",
                  dataIndex: "name",
                  ellipsis: true,
                  render: (value) => <Tooltip title={value}>{value}</Tooltip>,
                },
                {
                  title: "估算行",
                  dataIndex: "estimated_rows",
                  width: 92,
                  align: "right",
                  render: formatNumber,
                },
              ]}
              locale={{ emptyText: connectionReady ? <Empty description="没有表" /> : <Empty description="请先连接数据库" /> }}
            />
          </div>
        </Sider>

        <Content className="center-pane">
          <div className="toolbar-strip">
            <Segmented
              value={syncStrategy}
              options={[
                { label: "普通同步", value: "offset" },
                { label: "大表模式", value: "cursor" },
              ]}
              onChange={changeSyncStrategy}
            />
            <Space className="primary-actions">
              <Button title="生成计划" icon={<CloudSyncOutlined />} onClick={buildPlan} loading={busy}>
                生成计划
              </Button>
              <Button title="Dry-run" icon={<FieldTimeOutlined />} onClick={() => startRun(true)} loading={busy}>
                Dry-run
              </Button>
              <Button title="开始同步" type="primary" icon={<PlayCircleOutlined />} onClick={() => startRun(false)} loading={busy}>
                开始同步
              </Button>
            </Space>
          </div>

          <div className="center-grid">
            <section className="panel settings-panel">
              <SectionHeader icon={<ThunderboltOutlined />} title="同步设置" />
              <Form
                form={syncForm}
                layout="vertical"
                initialValues={DEFAULT_SYNC_VALUES}
              >
                <Form.Item name="sync_strategy" hidden>
                  <Input />
                </Form.Item>
                <div className="sync-form-grid">
                  <Form.Item label="写入模式" name="mode">
                    <Select
                      options={[
                        { label: "replace 覆盖", value: "replace" },
                        { label: "upsert 插入或更新", value: "upsert" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item label="分页大小" name="batch_size">
                    <InputNumber min={1} />
                  </Form.Item>
                  <Form.Item label="游标字段" name="cursor_field">
                    <Input placeholder={syncStrategy === "cursor" ? "默认主键" : "大表模式使用"} disabled={syncStrategy !== "cursor"} />
                  </Form.Item>
                  <Form.Item label="增量字段" name="incremental_field">
                    <Input placeholder="updated_at" />
                  </Form.Item>
                  <Form.Item label="增量起点" name="incremental_since">
                    <Input placeholder="2026-07-01 00:00:00" />
                  </Form.Item>
                  <Form.Item label="分片数" name="shard_count">
                    <InputNumber min={1} max={64} disabled={syncStrategy !== "cursor"} />
                  </Form.Item>
                  <Form.Item label="并发数" name="worker_count">
                    <InputNumber min={1} max={8} disabled={syncStrategy !== "cursor"} />
                  </Form.Item>
                  <Form.Item label=" " name="create_missing_tables" valuePropName="checked">
                    <Checkbox>缺表自动建表</Checkbox>
                  </Form.Item>
                  <Form.Item label=" " name="skip_exact_count" valuePropName="checked">
                    <Checkbox>跳过精确 count</Checkbox>
                  </Form.Item>
                </div>
                <Form.Item label="where 条件" name="where_clause">
                  <Input.TextArea rows={3} placeholder="例如 created_at >= '2026-07-01'" />
                </Form.Item>
              </Form>
            </section>

            <section className="panel plan-panel">
              <SectionHeader
                icon={<BranchesOutlined />}
                title="同步计划"
                extra={plan ? `${formatNumber(plan.total_rows)} 行 / ${plan.table_count} 表` : "尚未生成"}
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
                    { title: "游标", dataIndex: "cursor_field", width: 90, render: (value) => value || "-" },
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
                label: "运行",
                children: (
                  <RunPanel
                    run={currentRun}
                    percent={runPercent}
                    onResume={resumeRun}
                  />
                ),
              },
              {
                key: "jobs",
                label: "任务",
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
                label: "历史",
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
    </Layout>
  );
}

function SectionHeader({ icon, title, extra }) {
  return (
    <div className="section-header">
      <Space>
        {icon}
        <Text strong>{title}</Text>
      </Space>
      {extra ? <Text type="secondary">{extra}</Text> : null}
    </div>
  );
}

function RunPanel({ run, percent, onResume }) {
  if (!run) {
    return <Empty className="pane-empty" description="尚无运行" />;
  }
  const statusColor = run.status === "success" ? "success" : run.status === "failed" ? "error" : "processing";
  const shards = run.shards_state || [];
  return (
    <div className="run-panel">
      <Space direction="vertical" size={12} className="full-width">
        <Space className="run-title" align="center">
          <Badge status={statusColor} />
          <Text strong>{run.name}</Text>
          <Tag>{run.sync_strategy || "offset"}</Tag>
        </Space>
        <Progress percent={percent} status={run.status === "failed" ? "exception" : undefined} />
        <div className="metric-grid">
          <Statistic title="速度" value={run.rows_per_second || 0} precision={2} suffix="行/秒" />
          <Statistic title="已同步" value={Number(run.synced_gb || 0)} precision={4} suffix="GB" />
          <Statistic title="剩余" value={formatDuration(run.eta_seconds)} />
          <Statistic title="耗时" value={formatDuration(run.elapsed_seconds || 0)} />
        </div>
        {run.status === "failed" ? (
          <Button danger onClick={onResume}>
            继续
          </Button>
        ) : null}
        <List
          size="small"
          dataSource={run.tables_state || []}
          renderItem={(table) => (
            <List.Item>
              <List.Item.Meta
                title={
                  <Space>
                    <Text>{table.table_name}</Text>
                    <Tag>{table.status}</Tag>
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
                        <Tag>{shard.status}</Tag>
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
      </Space>
    </div>
  );
}

function JobsPanel({ jobs, jobForm, onSave, onRun, onLoad, busy }) {
  return (
    <div className="jobs-panel">
      <Form form={jobForm} layout="vertical" initialValues={{ schedule_enabled: false, cron_expr: "" }}>
        <Form.Item label="任务名称" name="name">
          <Input placeholder="例如 库存批次同步" />
        </Form.Item>
        <div className="job-save-grid">
          <Form.Item name="schedule_enabled" valuePropName="checked">
            <Checkbox>定时</Checkbox>
          </Form.Item>
          <Form.Item name="cron_expr">
            <Input placeholder="0 2 * * *" />
          </Form.Item>
          <Button icon={<SaveOutlined />} onClick={onSave} loading={busy}>
            保存
          </Button>
        </div>
      </Form>
      <List
        className="side-list"
        dataSource={jobs}
        locale={{ emptyText: <Empty description="暂无任务" /> }}
        renderItem={(job) => (
          <List.Item
            actions={[
              <Button key="load" size="small" onClick={() => onLoad(job)}>载入</Button>,
              <Button key="run" size="small" type="primary" onClick={() => onRun(job)}>运行</Button>,
            ]}
          >
            <List.Item.Meta
              title={job.name}
              description={`${job.tables.join(", ")} · ${job.sync_strategy === "cursor" ? "大表游标" : "普通分页"}`}
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
                <Tag>{run.status}</Tag>
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
          <Button onClick={onSave} loading={busy}>保存</Button>
          <Button type="primary" onClick={onLogin} loading={busy}>测试并登录</Button>
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
  return (
    <section className="connection-block">
      <SectionHeader icon={<ApiOutlined />} title={title} extra={passwordSet ? <Tag color="green">已保存密码</Tag> : null} />
      <div className="connection-form-grid">
        <Form.Item label="Host" name={[name, "host"]}>
          <Input />
        </Form.Item>
        <Form.Item label="Port" name={[name, "port"]}>
          <InputNumber min={1} />
        </Form.Item>
        <Form.Item label="User" name={[name, "user"]}>
          <Input />
        </Form.Item>
        <Form.Item label="Password" name={[name, "password"]}>
          <Input.Password placeholder={passwordSet ? "留空沿用已保存密码" : ""} />
        </Form.Item>
        <Form.Item label="Database" name={[name, "database"]}>
          <Input />
        </Form.Item>
        <Form.Item label="Charset" name={[name, "charset"]}>
          <Input />
        </Form.Item>
      </div>
      <Button icon={<CheckCircleOutlined />} onClick={() => onTest(name)}>
        测试{name === "prod" ? "产品库" : "测试库"}
      </Button>
    </section>
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
