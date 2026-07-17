# DB Sync Console

本地数据库同步控制台，用来把产品环境数据库的指定表同步到测试环境。当前 provider 支持 MySQL，后续可以继续扩展 PostgreSQL、SQL Server 等数据库。第一版聚焦 `prod -> test`，支持表搜索勾选、`replace` / `upsert`、`where` 条件、同步计划、`dry-run`、分页、断点继续、日志、常用任务和 crontab 定时任务。

## 快速开始

```bash
cd /Users/gaochenjie/Documents/MC/db-sync-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

启动本地页面：

```bash
python -m sync_tool.cli serve --host 127.0.0.1 --port 8765
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，在“连接登录”里填写产品库和测试库连接，点击“测试并登录”。

连接信息会保存在本地：

```text
data/sync_console.db
```

页面不会回显密码；如果已经保存过密码，密码框留空再保存会沿用原密码。

## 可选高级配置

默认不需要 `config.json`。如果想调整分页默认值、日志目录、结构校验或禁止同步的表，可以复制示例：

```bash
cp config.example.json config.json
```

然后编辑：

- `app.page_size`：每页同步行数
- `app.strict_schema`：是否要求两边表结构一致
- `safety.blocked_tables`：禁止同步的表
- `safety.max_rows_without_where`：无 `where` 时的大表提醒阈值

## 命令行

命令行模式仍然使用 `config.json`，主要给自动化脚本使用；日常使用推荐本地 Web 页面。

生成计划：

```bash
python -m sync_tool.cli --config config.json plan \
  --tables users,orders \
  --mode replace \
  --where "created_at >= '2026-07-01'"
```

执行同步：

```bash
python -m sync_tool.cli --config config.json sync \
  --tables users,orders \
  --mode upsert \
  --batch-size 1000
```

只做 dry-run：

```bash
python -m sync_tool.cli --config config.json sync \
  --tables users \
  --mode replace \
  --dry-run
```

继续失败的运行：

```bash
python -m sync_tool.cli --config config.json resume <run_id>
```

## 同步模式

`replace`

- 没有 `where`：先 `TRUNCATE TABLE test_table`，再分页插入产品库数据
- 有 `where`：先在测试库 `DELETE FROM table WHERE ...`，再插入产品库同条件数据
- 如果勾选“缺表自动建表”，测试库缺表时会先用产品库 `SHOW CREATE TABLE` 的结构创建表

`upsert`

- 从产品库分页读取
- 写入测试库时执行 `INSERT ... ON DUPLICATE KEY UPDATE`
- 需要表有主键
- 如果勾选“缺表自动建表”，测试库缺表时会先建表，再执行插入/更新

`dry-run`

- 只检查计划，不写入测试库
- 即使勾选“缺表自动建表”，dry-run 也不会真的创建表

## 大表同步模式

20GB 级别的大表建议使用“大表游标”：

- `同步方式`：选择“大表游标”
- `游标字段`：留空默认使用第一主键；也可以手动填写稳定递增且有索引的字段
- `增量字段`：例如 `updated_at`
- `增量起点`：例如 `2026-07-01 00:00:00`
- `跳过精确 count`：大表建议勾选，计划会使用数据库估算行数
- `分片数`：默认 2，数值型游标会按 min/max 切分
- `并发数`：默认 2，最大 8

大表模式按游标推进：

```sql
SELECT *
FROM table
WHERE id > last_pk
ORDER BY id
LIMIT 5000;
```

断点会记录到本地 `run_shards.last_pk`。失败后点击“继续”，会从每个分片已保存的 `last_pk` 继续。

注意：

- 分片并发只对数值型游标字段生效；非数值游标会自动降级为单分片游标同步
- 游标字段最好是主键或唯一递增索引
- 增量条件会和 `where` 同时生效
- 页面显示的 GB 是按实际拉取行内容估算，用于观察速度和趋势

## 断点和日志

运行状态存放在：

```text
data/sync_console.db
```

每次运行还会写一个日志文件：

```text
logs/<run_id>.log
```

同步按页提交。失败后页面会显示“继续”按钮，或使用 CLI `resume <run_id>` 继续。

## 定时任务

保存常用任务时勾选“定时”，填写 crontab 表达式，例如：

```text
0 2 * * *
```

表示每天 02:00 执行。定时任务只在本地服务运行时生效。

## 安全建议

- 产品库账号使用只读权限
- 第一次同步大表前先生成计划或 dry-run
- 对有隐私字段的表，后续建议增加脱敏规则
- `where` 会拦截分号和 SQL 注释，但仍然会作为 SQL 条件执行，只给可信用户使用
