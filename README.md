# 同步犬 SyncDog

同步犬是一款本地数据库同步工具，用来把生产环境数据库中的指定表同步到测试环境。它的目标不是替代 Navicat 的所有能力，而是把“从线上挑几张表、带条件、可预览、可断点、可重复执行”的同步流程做得更省事、更可控。

当前版本支持 MySQL，后续可以继续扩展 PostgreSQL、SQL Server 等数据库。

## 主要能力

- 本地 Mac 桌面应用，安装后即可打开使用
- 像 Navicat 一样在界面里配置产品库和测试库连接，不强制使用 `config.json`
- 表搜索、跨筛选勾选、多表同步
- 同步前生成计划，支持 `dry-run`
- `replace` 覆盖写入和 `upsert` 插入或更新
- 支持 `where` 条件和 `updated_at` 增量条件
- 测试库字段优先：只同步两边共有字段，保留测试库独有字段
- 大表模式：主键游标分页、`last_pk` 断点、跳过精确 count、分片并发
- 运行进度：行数、速度、已同步 GB、预计剩余时间、分片状态
- 常用同步任务保存和本地定时任务
- 本地日志和历史运行记录

## 下载安装

Apple Silicon Mac 可以直接下载：

[SyncDog-0.1.3-arm64.dmg](https://github.com/pdxjie/db-sync-console/releases/download/v0.1.3/SyncDog-0.1.3-arm64.dmg)

安装方式：

1. 下载并打开 DMG
2. 将“同步犬”拖到 Applications
3. 首次打开如果被 macOS 拦截，右键 App 选择“打开”，或到“系统设置 > 隐私与安全性”允许打开

这个安装包已经内置后端运行时，使用者不需要安装 Python、Node.js、npm 或 MySQL 客户端。

注意：当前公开安装包是 `arm64` 版本，适用于 Apple Silicon Mac。Intel Mac 需要单独构建 `x64` 包。当前 App 还没有做 Developer ID 签名和公证，所以首次打开会有 macOS 安全提示。

## 快速使用

1. 打开同步犬
2. 点击右上角“连接”
3. 填写产品库和测试库连接信息
4. 点击“测试并登录”
5. 在左侧搜索并勾选需要同步的表
6. 选择同步模式、分页大小、where 条件或大表模式参数
7. 点击“生成计划”确认影响范围
8. 需要演练时点击 `Dry-run`
9. 确认后点击“开始同步”

建议产品库账号使用只读权限，测试库账号使用只给目标库授权的写入账号。

## 同步模式

`replace` 表示覆盖目标数据：

- 没有 `where` 时，先清空测试库目标表，再插入产品库数据
- 有 `where` 时，只删除测试库中符合条件的数据，再插入产品库同条件数据
- 适合测试库需要和生产库某个范围保持一致的场景

`upsert` 表示插入或更新：

- 根据测试库目标表的主键或唯一键判断是否已存在
- 不存在则插入，存在则更新共有字段
- 适合测试库已有数据，需要补齐或刷新一部分生产数据的场景

`dry-run` 表示只演练：

- 创建运行记录和计划
- 不会写入测试库
- 不会创建表，也不会删除或更新数据

## 表结构不一致

同步犬默认以测试库结构为准，不会为了同步数据而强行改测试库字段。

- 产品库和测试库同名字段：参与同步
- 产品库有、测试库没有的字段：跳过
- 测试库有、产品库没有的字段：保留，不写入
- 同名字段类型不一致：计划里提示 warning，执行时交给 MySQL 转换

如果测试库独有字段是 `NOT NULL` 且没有默认值，新插入数据可能失败。这种情况建议给测试库字段设置默认值，或使用 `upsert` 同步已有行。

如果需要恢复严格校验行为，可以在可选配置里设置：

```json
{
  "app": {
    "strict_schema": true
  }
}
```

## 大表模式

20GB 级别的大表不要使用 offset 分页，建议启用“大表模式”。大表模式会按游标字段推进：

```sql
SELECT *
FROM table
WHERE id > last_pk
ORDER BY id
LIMIT 5000;
```

推荐设置：

- `游标字段`：默认主键，也可以选择稳定递增且有索引的字段
- `增量字段`：通常是 `updated_at`
- `增量起点`：例如 `2026-07-01 00:00:00`
- `跳过精确 count`：大表建议开启，避免 `COUNT(*)` 很慢
- `分片数`：默认 2，数值型游标可以按范围切片
- `并发数`：默认 2，最大 8
- `分页大小`：大表建议 5000 起步，再根据数据库压力调整

断点记录从 offset 改为 `last_pk`。同步失败后点击“继续”，会从每个表或分片保存的 `last_pk` 继续。

注意：

- 分片并发只适合数值型游标字段
- 游标字段最好是主键或唯一递增索引
- `where` 条件和 `updated_at` 增量条件会同时生效
- 页面显示的 GB 是按已拉取行内容估算，用于观察趋势

## 常用任务和定时同步

在“任务”区域可以保存当前同步配置，后续一键载入或直接运行。

启用定时任务时填写 crontab 表达式，例如：

```text
0 2 * * *
```

表示每天 02:00 执行。定时任务只在同步犬正在运行时生效；App 关闭后不会在后台继续执行。

## 本地数据位置

连接配置、任务、运行记录和断点都保存在本机，不上传到任何外部服务。

桌面 App 默认路径：

```text
~/Library/Application Support/sync-dog/data/sync_console.db
~/Library/Application Support/sync-dog/logs/
```

页面不会回显已保存密码。密码框留空保存时，会沿用本地已经保存的密码。

## 安全建议

- 产品库使用只读账号
- 测试库账号只授予必要库和表的权限
- 首次同步大表前先生成计划或 dry-run
- 对包含隐私信息的表，后续应增加脱敏规则
- 不要把生产库高权限账号发给其他人
- 确认云数据库白名单、安全组、VPN 和网络策略允许当前电脑访问

`where` 条件会拦截分号和 SQL 注释，但它仍然会作为 SQL 条件执行。这个工具适合可信团队内部使用，不建议开放给不可信用户随意填写 SQL。

## 开发环境

开发模式需要 Python 和 Node.js。桌面安装包不需要这些环境。

```bash
cd db-sync-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-build.txt
npm install
```

启动本地 Web 版：

```bash
python -m sync_tool.cli serve --host 127.0.0.1 --port 8765
```

启动桌面开发版：

```bash
npm run desktop
```

打包桌面 App：

```bash
npm run desktop:dist
```

打包脚本会先构建 React renderer，再用 PyInstaller 生成 `syncdog-backend`，最后用 Electron Builder 生成 DMG。

如果 Electron 下载较慢，可以使用镜像：

```bash
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm install
```

如果本机 `~/.npmrc` 权限导致打包失败，可以临时绕开：

```bash
NPM_CONFIG_USERCONFIG=/dev/null npm_config_userconfig=/dev/null ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm run desktop:dist
```

## 命令行

命令行主要用于自动化脚本。日常使用推荐桌面 App。

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

dry-run：

```bash
python -m sync_tool.cli --config config.json sync \
  --tables users \
  --mode replace \
  --dry-run
```

继续失败任务：

```bash
python -m sync_tool.cli --config config.json resume <run_id>
```

## 可选配置

桌面 App 默认不需要 `config.json`。如果要调整全局行为，可以复制示例：

```bash
cp config.example.json config.json
```

常用配置：

- `app.page_size`：默认分页大小
- `app.strict_schema`：是否要求两边表结构严格一致
- `safety.blocked_tables`：禁止同步的表
- `safety.max_rows_without_where`：无 where 时的大表提醒阈值

## 当前限制

- 当前 provider 只支持 MySQL
- 当前公开安装包只提供 macOS arm64
- App 未签名、未公证，首次打开需要手动允许
- 暂不支持 SSH Tunnel、代理、SSL 证书配置界面
- 暂不支持字段脱敏规则
- 定时任务依赖 App 正在运行

## 适合场景

同步犬适合这些工作：

- 从生产库同步指定几张表到测试库
- 用 where 条件只同步某个时间范围或业务范围
- 对大表做可断点的游标同步
- 保存常用同步任务，反复执行
- 在测试库结构和生产库结构不完全一致时，只同步共有字段

它不适合直接做跨环境全库迁移、生产数据备份、数据库结构发布或面向公网的自助 SQL 平台。
