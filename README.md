# BidVolt 投标工作台

面向招投标场景的智能辅助平台：招标材料解析（含国标 OFD）、企业资料库、Requirement 管理、
成果（商务标/技术标/报价单）生成与校核、逐条评审闭环、确定性报价测算、AnySearch 联网检索、
Hermes Agent 工具调用，以及用于验证的 Demo 前端。

> 部署形态：**单个 Docker 容器**（平台直接提供，不走 Dockerfile）；数据与可部署产物全部落在
> 平台外挂持久卷 `/data`，容器重建后无需重装依赖即可恢复。

---

## 1. 功能总览

- 登录/会话：JWT 登录、刷新、企业上下文、RLS 租户隔离
- 项目工作台：项目 CRUD/归档、材料上传（txt/docx/xlsx/pdf/**OFD**）、解析状态
- 项目快照：列表 + 详情（不可变 manifest：材料哈希/要求版本/成果版本/规则与模型版本）；活动任务查询
- Requirement：提取/确认/修订/证据定位（文件坐标）
- 成果版本链：商务标/技术标/报价单生成、校核、CAS 版本冲突保护、**指定版本下载**（DOCX/XLSX）
- 评审闭环：逐条 review_item、材料补充、单条/批量确认、重审、仅改受影响项
- 报价：历史价外部只读 Provider、确定性 QuoteEngine、冻结样本可复算、AI 只给参考区间
- 项目助手：会话 + 消息历史（刷新可恢复），LLM 回答带上下文入库
- 搜索：AnySearch 真实接入（官方 JSON-RPC 端点），DLP 脱敏 + 域名 trust_level 分级，
  无 Key 走匿名额度（约 50 次/天），正式 Key 由运维在 Secret Manager 配置
- 云模型：MiniMax 文本（LLM）、百炼 DashScope qwen-vl（视觉），受 P1 门禁控制
- Hermes Agent：NousResearch Hermes（`/data/hermes`），连接 bidvolt MCP（24 个工具）+ 5 个业务 Skill
- Demo 前端：`/demo/`（真实调用后端 API，用于逐项验证）

## 2. 架构

```
浏览器（Demo 前端 /demo/ 或业务前端）
   │ HTTP + SSE
   ▼
FastAPI（app.main，uvicorn :8123）
   ├─ PostgreSQL 14（/data/pgdata，RLS 租户隔离）      ← supervisor 守护
   ├─ Worker（app.services.worker，任务队列）            ← supervisor 守护
   ├─ ClamAV（病毒扫描，fail-closed）                    ← supervisor 守护
   ├─ Hermes Agent（/data/hermes，headless gateway :9119）← supervisor 守护
   │    └─ bidvolt MCP（stdio，24 工具）→ 后端
   └─ AnySearch / MiniMax / DashScope（出网，DLP 门禁）
```

单容器内由 **supervisord** 统一管理 postgres / app / worker / hermes / clamd / backup-cron，
崩溃自动重启；容器入口为 sshd（无 systemd），部署脚本安装 `/etc/ssh/sshrc` 兜底：
SSH 登录时若 supervisord 未运行则自动拉起。

## 3. 目录结构

```
app/             FastAPI 应用（api/services/models/static demo）
bidvolt_mcp/     MCP stdio server（OpenRPC IDL + 24 个工具）
deploy/          容器部署脚本（install.sh / bidvolt-init.sh / supervisord.conf / install-hermes.sh / backup.sh）
docs/            架构/模块/威胁模型/数据分级授权清单/Hermes 契约与 Skill
scripts/         冒烟与工具脚本（真实 OFD 样本、云能力线上冒烟、MCP 契约）
tests/           pytest（unit/module，容器上跑 PG+RLS）
```

## 4. 本地开发（快速开始）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # 填入数据库/密钥/门禁
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8123
.venv/bin/python -m app.services.worker   # 另开终端
```

测试：

```bash
.venv/bin/pytest -q                 # 本地全量（SQLite）
```

## 5. 部署（运维）

### 5.1 路径约定（全部配置化，不依赖符号链接）

部署脚本通过环境变量决定路径，`deploy/supervisord.conf` 中的 `__REPO__`/`__HERMES_HOME__`
由 `install.sh` 用 `sed` 生成到 `/etc/supervisor/conf.d/bidvolt.conf`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPO` | `/data/bidvolt` | 代码 + venv（唯一真源，容器重建后仍在） |
| `HERMES_HOME` | `/data/hermes` | Hermes Agent 数据 + venv |
| `PGDATA` | `/data/pgdata` | PostgreSQL 数据目录（init 脚本固定） |
| `STORAGE_ROOT` | `/data/appdata` | 上传/成果文件（租户前缀目录） |
| `BACKUP_ROOT` | `/data/backups` | 每日 pg_dump + WAL 归档 |
| 日志 | `/data/logs/bidvolt` | `/var/log/bidvolt` 符号链接（init 脚本建立） |

### 5.2 首次安装

```bash
# 本地：把仓库与 .env 复制到容器
scp -r . root@<host>:/data/bidvolt
scp .env root@<host>:/data/bidvolt/.env

# 容器内一键安装（apt 系统包 + venv + PG 初始化 + supervisor + Hermes）
ssh root@<host>
bash /data/bidvolt/deploy/install.sh
```

`install.sh` 完成：系统包（PostgreSQL/ClamAV/LibreOffice/supervisor）→ venv 与依赖 →
`/data` 子目录 → 生成 supervisor 配置（路径来自 `REPO`/`HERMES_HOME`）→ 初始化 PG/迁移 →
启动 supervisord → 安装 Hermes Agent（`SKIP_HERMES=1` 可跳过）。

### 5.3 容器重启 / 重建后恢复

- **同实例重启**：容器可写层与 `/data` 都在，服务由 supervisord 守护；若平台重启后没有自动
  拉起进程，SSH 登录一次即触发 `/etc/ssh/sshrc` 兜底自动恢复。
- **容器被销毁重建**：`/data` 全部保留（代码/venv/Hermes/数据库/文件/备份/日志），
  只需重新执行 `bash /data/bidvolt/deploy/install.sh`（apt 重装系统包 + 生成配置 + 启动；
  venv 已存在时跳过创建，PG 检测到数据目录直接迁移启动）。

### 5.4 验证

```bash
bash /usr/local/bin/bidvolt-healthcheck        # PG + API 健康
curl http://127.0.0.1:8123/healthz             # {"status":"ok"}
supervisorctl status                            # postgres/app/worker/hermes/clamd/backup-cron
```

### 5.5 备份与恢复

- 每日 02:00 cron `pg_dump -Fc` 到 `/data/backups/bidvolt_<日期>.dump`，保留 30 天；
  WAL 归档到 `/data/backups/wal`（PITR）。注意 `/etc/cron.d` 条目必须带 `root` 用户字段。
- 恢复：`pg_restore -d bidvolt /data/backups/bidvolt_xxx.dump`。

### 5.6 日常运维

- 重启单服务：`supervisorctl restart app|worker|hermes`
- 改配置：`supervisorctl reread && supervisorctl update`
- 日志：`/data/logs/bidvolt/`（supervisord.log、backup.log）
- 升级代码：容器内 `git pull && .venv/bin/pip install -r requirements.txt && supervisorctl restart all`
- **云能力开关**：`DATA_CLASSIFICATION_CONFIRMED`/`CLOUD_LLM_ENABLED`/`SEARCH_ENABLED` 修改
  `/data/bidvolt/.env` 后，需重启容器（supervisor 继承启动时的环境变量，优先级高于 .env）；
  或把开关固定进 supervisor `environment=` 后 `update`。

## 6. 环境变量（.env，禁止提交 git）

见 [.env.example](.env.example)。关键项：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` / `APP_DB_PASSWORD` | PostgreSQL 连接与建库密码 |
| `JWT_SECRET` / `BIDVOLT_INTERNAL_TOKEN` | 签名密钥与内部传输令牌（≥32 位随机串） |
| `MINIMAX_API_KEY` / `MINIMAX_BASE_URL` | MiniMax 文本模型（默认 api.minimax.chat/v1） |
| `DASHSCOPE_API_KEY` | 百炼 qwen-vl 视觉模型 |
| `ANYSEARCH_KEY` / `ANYSEARCH_BASE_URL` | 搜索（空 = 匿名额度；端点默认 api.anysearch.com/mcp） |
| `DATA_CLASSIFICATION_CONFIRMED` | P1 门禁总开关（签署清单后才置 1） |
| `CLOUD_LLM_ENABLED` / `SEARCH_ENABLED` | 云模型/搜索子开关 |
| `SEARCH_MODE` | `mock`（离线合成）/ `anysearch`（真实出网） |
| `STORAGE_ROOT` / `BACKUP_ROOT` | 文件与备份根目录（/data 约定） |
| `VIRUS_SCAN_REQUIRED` | 1 = ClamAV 不可用即拒绝入库（fail-closed） |

## 7. 使用说明（产品侧）

1. 注册/登录 → 进入工作台
2. 新建项目 → 上传招标材料（txt/docx/xlsx/pdf/OFD；真实 OFD 含文本层/签章/扫描版兜底）
3. 解析后生成 Requirement（可逐条确认/修订，带原文坐标证据）
4. 选择企业资料/报价参数 → 生成三份成果（版本链 + CAS 冲突保护）
5. 发起评审：逐条 review_item（问题/得分/可提升分/依据），可单条或批量补充材料并确认，
   重新审核只重跑受影响项；"一键提升"不改分，只发起材料解析+确认流程
6. 报价测算：确定性 QuoteEngine（冻结样本可复算），AI 只给参考区间并标注风险，用户确认后落版本
7. 搜索：工作台搜索框调用 AnySearch（DLP 脱敏，结果按 trust_level 分级）
8. 助手/Agent：Hermes 可通过 bidvolt MCP 读写项目数据、执行 5 个 Skill 流程

## 8. 安全与门禁

- P1 门禁：`docs/数据分级与授权确认清单.md` 签署前，云模型/搜索默认关闭（fail-closed）
- 租户隔离：全部业务对象绑定 enterprise_id（+ project_id），PG RLS 纵深防护
- 任务级授权：capability token 绑定 enterprise/project/task/工具白名单（`X-Bidvolt-Cap`）
- 文件安全：隔离区 → magic bytes → 强制 ClamAV → 受限进程解析；压缩包限额/符号链接拒绝
- 出站：搜索前 DLP 脱敏（手机号/证件号/银行卡/信用代码）+ 域名白名单
- SSE/日志：白名单事件，不输出思维链/工具参数/凭据/错误栈

## 9. Hermes Agent（已部署）

- 位置：`/data/hermes`（venv + 数据 + skills），supervisor `[program:hermes]` 守护
  `hermes serve` headless gateway（127.0.0.1:9119）
- 模型：主推理 MiniMax-Text-01（`minimax` provider，max_tokens=8000）；
  视觉辅助 = 百炼 qwen-vl-max（`auxiliary.vision` custom + DashScope 兼容端点）
- MCP：`bidvolt`（24 工具，stdio），调用后端需 `BIDVOLT_INTERNAL_TOKEN`
  （当前为 Hermes 服务账号 JWT；生产按 P0-2 走任务级 capability token）
- Skills：`bidvolt-tender-parse / material-match / bid-generate / mock-evaluate / targeted-edit`
- 常用命令（容器内，`export HERMES_HOME=/data/hermes`）：

```bash
/data/hermes/venv/bin/hermes -z '调用 bidvolt MCP 的 search_web 工具搜索：政府采购 电缆 中标价' --yolo --cli
/data/hermes/venv/bin/hermes skills list
/data/hermes/venv/bin/hermes mcp list
```

重新安装：`bash /data/bidvolt/deploy/install-hermes.sh`（幂等）。

## 10. 测试与验证

```bash
# 本地（SQLite）
.venv/bin/pytest -q

# 容器（PostgreSQL + RLS，加载 .env）
bash /tmp/run_container_tests.sh -q

# 线上冒烟
.venv/bin/python scripts/smoke_cloud_live.py            # AnySearch 匿名 + MiniMax LLM
.venv/bin/python scripts/smoke_ofd_live.py --file <真实OFD>  # 真实 OFD 上传解析
.venv/bin/python scripts/fetch_ofd_samples.py           # 下载真实 OFD 样本（复现用）
```

当前基线：本地 170 passed；容器 171 passed（PG+RLS）。

## 11. 已知限制 / 路线图

- Hermes 任务级授权：当前 MCP 调用使用服务账号 JWT；生产需接入"任务创建 → capability token →
  Hermes 执行 → 白名单进度"完整闭环
- 在线 Word/Excel 编辑会话、项目助手会话历史、快照列表/明细等见
  [Issue #2 功能清单](https://github.com/zhangsheng377/BidVolt/issues/2)
- AnySearch 中文检索质量受上游服务限制；匿名额度 50 次/天，正式 Key（1000 次/天）由运维配置
- 扫描版 OFD/图片走视觉模型兜底（qwen-vl），需在业务侧确认视觉门禁与授权
