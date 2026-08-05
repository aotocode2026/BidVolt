# Hermes Agent 集成深化设计

> 所属：后端架构设计文档 4.4 / 模块细化设计 4.4
> 内容：bidvolt MCP 工具契约 + 5 个业务 Skill 的 SKILL.md 草稿
> 依据：Hermes Agent 官方 SKILL.md 格式与 MCP 配置规范

## 目录

- [README.md](./README.md) — 本总览
- [bidvolt-mcp-tools.md](./bidvolt-mcp-tools.md) — 业务 MCP 工具契约（Hermes ↔ 后端能力接口）
- [skills/](./skills/) — 5 个业务 Skill：
  - `tender-parse/SKILL.md` 招标解析
  - `material-match/SKILL.md` 资料匹配
  - `bid-generate/SKILL.md` 标书生成/校核
  - `mock-evaluate/SKILL.md` 模拟评标
  - `targeted-edit/SKILL.md` 针对性修改

## 1. 集成架构回顾

```
后端业务服务（BidVolt API）
   │  HTTP + SSE（任务提交、消息透传）
   ▼
Hermes Agent（独立容器）
   ├─ bidvolt MCP server（stdio，本仓库实现）→ 业务数据能力
   ├─ anysearch MCP server → 网络搜索
   ├─ 模型：qwen-max（主推理）/ qwen-vl-max（vision_model）/ qwen-plus（轻量）
   └─ Skills：5 个业务 SKILL.md（本目录）
```

## 2. 关键配置（config.yaml 片段）

```yaml
providers:
  default: deepseek           # 主推理 provider
  dashscope: {}               # 视觉模型 provider（qwen-vl）
models:
  main: deepseek-v4-flash     # 主推理
  vision: qwen-vl-max         # 扫描件/图片理解（4.3）
  cheap: deepseek-v4-flash    # 轻量任务

mcp_servers:
  bidvolt:
    command: python
    args: ["-m", "bidvolt_mcp"]          # 本仓库实现的 stdio MCP server
    env:
      BIDVOLT_API_BASE: "http://bidvolt-api:8000"
      BIDVOLT_INTERNAL_TOKEN: "${BIDVOLT_INTERNAL_TOKEN}"
    supports_parallel_tool_calls: true   # 三份成果可并行生成
  anysearch:
    command: python
    args: ["-m", "anysearch_mcp"]
    env:
      ANYSEARCH_KEY: "${ANYSEARCH_KEY}"  # 缺省降级匿名（约 50 次/天）

skills:
  enabled:
    - bidvolt-tender-parse
    - bidvolt-material-match
    - bidvolt-bid-generate
    - bidvolt-mock-evaluate
    - bidvolt-targeted-edit
```

> 说明：`BIDVOLT_API_BASE` 指向后端业务服务；内部 token 走 `.env`（`${VAR}` 在连接时替换）。所有 MCP 工具调用由后端按 Profile/Session 注入 `enterprise_id` / `project_id`，Hermes 不需要传租户参数。

## 3. 能力边界原则（写入 MCP 与 Skill 的共同约束）

| # | 原则 | 落地 |
|---|---|---|
| P1 | 禁止编造企业事实 | 生成/校核 Skill 强制要求：所有企业信息（资质/业绩/人员/参数/成本）必须来自 `search_assets` 查询结果，无来源内容不得写入成果 |
| P2 | 报价只建议不落库 | Hermes 只有 `calculate_quote`（建议），无 `apply_quote`；应用只能由用户在前端确认（后端接口，4.7） |
| P3 | 修改必须可追溯 | 所有写版本操作记录 `source_task_id`，走 4.5 版本链 |
| P4 | 证据可定位 | 评分/建议必须携带 evidence（坐标/节点/材料/URL），无证据项不计入可解释总分 |
| P5 | 搜索来源分级 | trust_level=3（低可信）不得直接写入正文，必须提示核实 |
| P6 | 进度白名单事件（产品决策 D-E） | 后端将 Hermes 输出**过滤后**以白名单事件推送前端：phase/status/percent/当前工作/简短依据/操作提示；**禁止透传思维链、工具参数、返回值、内部ID、凭据、错误栈**；错误只显示用户可理解的失败状态，详情进服务端日志 |
| P7 | 任务级授权（产品决策 D-B） | 每个任务签发授权上下文（enterprise/project/task/工具白名单/对象范围），MCP 按上下文校验；企业资料写工具仅企业资料导入任务可用；禁止静态 Token 全权限 |

## 4. 部署与验收

- Skill 目录部署：Hermes 的 skills 目录（官方 `skills/` 或用户级 `~/.hermes/skills/`）放入本仓库 `docs/hermes/skills/*/SKILL.md`
- 验收命令：`hermes chat --toolsets skills -q "用 招标解析 skill 解析上传的材料"`（官方推荐测试方式）
- MCP 验收：`hermes mcp configure bidvolt` 检查工具清单是否正确加载
