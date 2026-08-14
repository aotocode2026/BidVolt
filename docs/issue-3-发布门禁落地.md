# Issue #3 发布门禁落地记录（2026-08-14）

> 对应 Issue：https://github.com/zhangsheng377/BidVolt/issues/3
> 本文件记录本地已完成的整改与剩余需在真实服务器验收的事项。

---

## 一、已落地（本地代码/配置层面）

### 1. 敏感信息扫描与 CI 门禁

- 全历史扫描：`gitleaks detect --source . --log-opts=--all` 对 70 个 commit 全量扫描，
  **0 泄漏**（2026-08-14，gitleaks v8.30.1 默认规则）。
- `.pre-commit-config.yaml`：
  - pre-commit：gitleaks 暂存区扫描 + 基础卫生（trailing/EOF/yaml/大文件/换行/debug 语句）；
  - pre-push：gitleaks 全历史扫描 + pytest 全量（需本地 `pre-commit install --hook-type pre-push`）。
- `.github/workflows/ci.yml`：push/PR 自动执行 gitleaks（全历史）+ ruff + compileall +
  `git diff --check` + pytest（SQLite）。
- `.gitignore` 扩充：私钥（`*.key/*.pem/*.crt/*.p12/*.pfx`）、数据库与备份制品
  （`*.dump/*.sql/*.db/*.sqlite*/*.bak`）、常见凭据文件（`credentials*`）。
- 泄露处置原则（写入部署手册）：发现泄露 → 立即吊销轮换密钥 → 清理历史，只删文件不算修复。

### 2. 敏感信息输出整改

- `scripts/debug_env.sh`：不再输出完整 `DATABASE_URL`（脱敏为 `user:***@host:port/db`），
  只输出 SET/UNSET 与长度；路径改为 `$REPO`（默认 `/data/bidvolt`，旧 `/opt` 引用全部清理）。
- `deploy/bidvolt-init.sh` / `scripts/remote_bootstrap.sh`：数据库口令不再拼入 `psql -c`
  命令行/SQL 字符串——改为 0600 临时 SQL 文件 + psql `\set`/`:'pw'` 变量传入，
  避免进程参数暴露、特殊字符注入问题；用后即删。
- `.env` 权限由脚本强制：`install.sh`/`bidvolt-init.sh`/`fix_env.sh` 校验并 `chmod 600`，
  不再只停留在文档。
- `scripts/run_container_tests.sh` 输出 `DATABASE_URL` 前先脱敏。

### 3. 配置 fail-fast（生产弱默认值禁止启动）

- `app/config.py`：新增 `BIDVOLT_ENV`（默认 `dev`）。`BIDVOLT_ENV=production` 时启动校验：
  - 拒绝开发默认 DB URL / 弱数据库口令 / SQLite 主库；
  - 拒绝 `change_me`/`dev-only` 类或长度 <32 的 `JWT_SECRET`；
  - 拒绝未设置/长度 <32 的 `BIDVOLT_INTERNAL_TOKEN`；
  - 拒绝 `VIRUS_SCAN_REQUIRED=0`（ClamAV fail-closed 强制）。
- `deploy/bidvolt-init.sh`：启动前 fail-fast（空/过短/占位密码与 JWT_SECRET），
  <32 位密钥告警提示轮换。
- 测试：`tests/unit/test_config_prod.py`（7 个用例：弱默认/短密钥/占位令牌/SQLite/关闭 ClamAV/
  强配置通过/dev 宽松）。

### 4. 任务中断恢复（worker 租约 + 心跳 + 回收）

- `task` 表新增 `lease_owner` / `lease_expires_at` / `last_heartbeat_at`（alembic 0015）。
- worker 领取任务即置租约（默认 300s），执行期间独立会话心跳续期（60s，
  PG 下 `lock_timeout=2s` 防长 handler 持锁排队）；终态/重新入队自动释放。
- `reclaim_stale()`：租约过期的 RUNNING 任务按失败计次——未达上限重新入队，
  达上限 FAILED_TERMINAL 留审计；优雅停机（取消/信号）同样走失败计次路径，
  **不再永久卡 RUNNING**。
- 测试：`tests/unit/test_task_service.py` 新增 5 个用例（租约置放/释放、过期回收、
  有效租约不误伤、重试耗尽终态、心跳续期）。
- `supervisord.conf`：全部程序增加 `startretries=5`/`startsecs=10`/`stopwaitsecs`/日志落盘，
  避免无限重启风暴掩盖真实错误。

### 5. 部署与升级流程

- 新增 `deploy/upgrade.sh`（安装为 `/usr/local/bin/bidvolt-upgrade`）：
  预检（磁盘/内存/密钥占位值）→ 记录基线 + `pip freeze` → 备份（pg_dump 校验 + appdata tar）
  → 新 venv 构建（不碰运行版本）→ 停 app/worker（**不重启 PostgreSQL**）→ alembic 迁移
  （失败即中止恢复旧版）→ 原子切换 venv → 冒烟（healthz + healthcheck + alembic head +
  supervisor RUNNING）→ 失败自动回滚 → 发布记录落 `/var/log/bidvolt/releases.log`。
- `deploy/install.sh` 修复首装顺序缺陷：Hermes 先于 supervisor 启动安装（原顺序下
  `bidvolt-init` 结尾 `exec supervisord` 前台常驻，Hermes 安装步骤永远执行不到）；
  增加 `.env` 预检；注释与手册统一为 `/data/bidvolt`。
- `deploy/healthcheck.sh` 扩展：PG + API + alembic head + worker 进程 + clamd socket +
  `/data` 独立挂载校验 + 磁盘阈值 + appdata 读写冒烟。
- `deploy/backup.sh` 扩展：pg_dump 生成后 `pg_restore --list` 校验可读；新增
  `/data/appdata` 业务文件 tar 备份；保留 30 天；WAL 归档 7 天清理策略。
- 部署手册：废弃 `git pull && pip install && supervisorctl restart all`；`scp -r .` 改为
  `git archive` 制品 + 密钥独立注入，避免带入 `.env`/`.git`。

## 二、剩余待办（需真实服务器验收，无法本地完成）

- [x] ~~服务器完成一轮升级流程~~ → 已按环境约束用归档方式完成真实部署（见第三节）；
- [ ] 完成一次备份恢复演练：`pg_restore` + appdata 解包到空环境，校验业务可恢复
      （dump/appdata 备份已带校验，演练待执行）；
- [ ] 验证容器/宿主机重启后 supervisor 自动拉起（sshrc 兜底）与 `/data` 挂载正确；
- [x] 将生产密钥轮换为 ≥32 位 → 已轮换为 48 位（旧会话失效，需重新登录）；
- [ ] 设置 `BIDVOLT_ENV=production` 启用 fail-fast（需先确认 ClamAV 强制扫描
      `VIRUS_SCAN_REQUIRED=1` 与数据分级签署，否则生产模式会按设计拒绝启动）；
- [x] CI（GitHub Actions）首次运行确认 gitleaks/ruff/pytest 全绿（sha 599084a 全部通过）；
- [ ] 数据分级与客户授权清单签署（P1 门禁，见 `docs/数据分级与授权确认清单.md`）。

## 三、真实服务器部署记录（2026-08-14 完成）

- 部署对象：`47.100.182.3`（SSH 16160 / 公网 28123），`/data` 为宿主机挂载卷。
- 发布前盘点：服务器 git HEAD 停在 `1df3c09`（工作树为新代码的脏状态），**无 GitHub 出网**
  （github.com:443 拒绝、SSH 远程无密钥）→ 改用本地 `git archive HEAD` 制品 + SFTP + 原地解包
  （挂载盘不支持目录 rename，整目录原子切换不可行；以整树 tar 备份作回滚点）。
- 密钥轮换：JWT_SECRET / BIDVOLT_INTERNAL_TOKEN 均轮换为 48 位随机串（旧会话全部失效需重新登录），
  Hermes `.env`/`config.yaml` 同步更新并重启生效。
- 迁移：生产 PostgreSQL 已执行 alembic `0015`（task 租约三列，纯加列），`current == head`。
- 过程中发现并修复的真实问题：
  1. ruff UP017 将 `timezone.utc` 改写为 `datetime.UTC`（Python 3.11+），**服务器 venv 为 Python 3.10**
     → app/worker 启动即崩（重启风暴）；已全量回退并 `ignore UP017`，本地/CI 双验证。
  2. 历史提交中的 shell 脚本带 CRLF → 服务器 bash 语法错误；已加 `.gitattributes`（eol=lf）+
     `git add --renormalize` 全库规整，部署脚本兜底 `sed -i 's/\r$//'`。
- 验证结果（服务器实测）：
  - `supervisorctl status`：postgres/app/worker/hermes/clamd/backup-cron 全部 RUNNING；
  - `bidvolt-healthcheck`（新版：PG/API/alembic head/worker/clamd/持久卷/磁盘/读写）→ HEALTH_OK；
  - 公网 `http://47.100.182.3:28123/healthz` → `{"status":"ok"}`；`/demo/` → 200；
  - 发布记录：`/var/log/bidvolt/releases.log`；回滚点：`/data/backups/bidvolt_tree_preupgrade_*.tgz`。

## 四、验证基线

- 本地 pytest：206 passed / 1 skipped / 1 deselected（2026-08-14，含新增租约与 fail-fast 用例）；
- 浏览器 E2E（headless Chromium，`scripts/e2e_browser_demo.py`）：**11/11 PASS**，截图
  `output/playwright/e2e-*.png`（注册→项目→上传解析→成果/校核/在线编辑→评标闭环→报价闭环→
  会话→终检导出→快照→刷新恢复）；
- ruff（E/F/I/UP，ignore UP017）：0 error；
- gitleaks 全历史：0 泄漏；
- 全部 shell 脚本 `bash -n` 通过；服务器迁移 current=head=0015；
- CI：gitleaks 全历史 / ruff / pytest-SQLite 三任务全部通过。

## 五、环境约束备忘（后续运维必读）

1. **服务器无 GitHub 出网**：`bidvolt-upgrade`（git 拉取版）不适用本服务器；本环境升级 =
   本地 `git archive` + SFTP + 原地解包（流程见第三节与 releases.log）。
2. **挂载盘不支持目录 rename**：禁止 `mv` 整目录切换；回滚 = 解包整树 tar。
3. **服务器 Python 3.10**：代码必须兼容 3.10（已 ignore UP017；CI 用 3.11，同时保持兼容写法）。
4. **shell 一律 LF**：`.gitattributes` 已强制，勿改。
