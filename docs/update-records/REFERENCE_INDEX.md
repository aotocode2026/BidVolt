# 反向索引

本文件用于按 issue、discussion、pull request 反查更新记录。
完整更新内容见 `UPDATE_LOG.md`。

排序规则：优先按“更新时间”倒序；同一时间再按“编号”升序，最后按“类型”（discussion → issue → pr）。同一个引用对应多条更新时，逐行列出，不合并、不省略。

| 更新时间 | 编号 | 类型 | 更新记录 |
|---|---|---|---|
| 2026-09-10 14:58 | #55 | discussion | [企业资料“源文件”分类与源包状态修复](UPDATE_LOG.md#2026-09-10-1458-feat-source-archive-category) |
| 2026-09-10 14:58 | #56 | issue | [企业资料“源文件”分类与源包状态修复](UPDATE_LOG.md#2026-09-10-1458-feat-source-archive-category) |
| 2026-09-10 11:31 | #51 | issue | [行情库原件/图片对所有登录用户可见](UPDATE_LOG.md#2026-09-10-1131-fix-market-file-download-isolation) |
| 2026-09-09 23:48 | #48 | issue | [企业资料分类状态信号（分类中/完成）](UPDATE_LOG.md#2026-09-09-2348-feat-enterprise-classification-status) |
| 2026-09-09 23:21 | #35 | discussion | [国网公告 ZIP 混合编码文件名兼容，修复误报损坏](UPDATE_LOG.md#2026-09-09-2321-fix-zip-mixed-encoding) |
| 2026-09-09 23:21 | #47 | issue | [国网公告 ZIP 混合编码文件名兼容，修复误报损坏](UPDATE_LOG.md#2026-09-09-2321-fix-zip-mixed-encoding) |
| 2026-09-09 20:19 | #45 | issue | [docx 图片 ContentType 与实际格式不一致导致图片无法显示](UPDATE_LOG.md#2026-09-09-2019-fix-docx-media-content-type) |
| 2026-09-09 11:57 | #38 | discussion | [Docker 崩溃恢复、启动自举修复与生成任务防复发](UPDATE_LOG.md#2026-09-09-1157-ops-docker-crash-recovery) |
| 2026-09-09 11:57 | #43 | issue | [Docker 崩溃恢复、启动自举修复与生成任务防复发](UPDATE_LOG.md#2026-09-09-1157-ops-docker-crash-recovery) |
| 2026-09-09 11:53 | #38 | discussion | [Agent 主会话任务状态一致性（终态/重试/回收）](UPDATE_LOG.md#2026-09-09-1153-fix-agent-task-state-consistency) |
| 2026-09-09 11:53 | #42 | issue | [Agent 主会话任务状态一致性（终态/重试/回收）](UPDATE_LOG.md#2026-09-09-1153-fix-agent-task-state-consistency) |
| 2026-09-09 10:33 | #37 | discussion | [配置 DeepSeek 凭据并恢复 Agent 主会话生成](UPDATE_LOG.md#2026-09-09-1033-ops-restore-deepseek-credential) |
| 2026-09-09 10:33 | #41 | issue | [配置 DeepSeek 凭据并恢复 Agent 主会话生成](UPDATE_LOG.md#2026-09-09-1033-ops-restore-deepseek-credential) |
| 2026-09-09 10:25 | #37 | discussion | [Agent 主会话模型凭据缺失快速失败](UPDATE_LOG.md#2026-09-09-1025-fix-agent-credential-fail-fast) |
| 2026-09-09 10:25 | #41 | issue | [Agent 主会话模型凭据缺失快速失败](UPDATE_LOG.md#2026-09-09-1025-fix-agent-credential-fail-fast) |
| 2026-09-08 22:43 | #26 | discussion | [行情库改为平台共享（所有用户可见）](UPDATE_LOG.md#2026-09-08-2243-fix-market-knowledge-platform) |
| 2026-09-08 22:43 | #34 | issue | [行情库改为平台共享（所有用户可见）](UPDATE_LOG.md#2026-09-08-2243-fix-market-knowledge-platform) |
| 2026-09-08 21:56 | #26 | discussion | [投标行情内容库管理与 Agent 生成前参考](UPDATE_LOG.md#2026-09-08-2156-feat-market-knowledge-library) |
| 2026-09-08 21:56 | #34 | issue | [投标行情内容库管理与 Agent 生成前参考](UPDATE_LOG.md#2026-09-08-2156-feat-market-knowledge-library) |
| 2026-09-08 20:56 | #27 | discussion | [招标公告 URL 导入开放任意网址并明确前端轮询契约](UPDATE_LOG.md#2026-09-08-2056-fix-tender-import-any-site) |
| 2026-09-08 20:56 | #32 | issue | [招标公告 URL 导入开放任意网址并明确前端轮询契约](UPDATE_LOG.md#2026-09-08-2056-fix-tender-import-any-site) |
| 2026-09-08 18:25 | #27 | discussion | [招标公告 URL 导入逐附件下载与预览](UPDATE_LOG.md#2026-09-08-1825-feat-tender-import-attachments) |
| 2026-09-08 18:25 | #32 | issue | [招标公告 URL 导入逐附件下载与预览](UPDATE_LOG.md#2026-09-08-1825-feat-tender-import-attachments) |
| 2026-09-07 23:45 | #15 | discussion | [worker 泵循环 MissingGreenlet 自愈与悬挂事务加固](UPDATE_LOG.md#2026-09-07-2345-fix-worker-greenlet-hardening) |
| 2026-09-07 23:45 | #25 | issue | [worker 泵循环 MissingGreenlet 自愈与悬挂事务加固](UPDATE_LOG.md#2026-09-07-2345-fix-worker-greenlet-hardening) |
| 2026-09-07 23:32 | #1 | discussion | [产物详情增加文件健康信号并优雅降级损坏文件](UPDATE_LOG.md#2026-09-07-2332-feat-artifact-file-health) |
| 2026-09-07 23:32 | #24 | issue | [产物详情增加文件健康信号并优雅降级损坏文件](UPDATE_LOG.md#2026-09-07-2332-feat-artifact-file-health) |
| 2026-09-07 23:22 | #1 | discussion | [上传批次补齐 ZIP 子文件关联与逐文件解析状态](UPDATE_LOG.md#2026-09-07-2322-fix-upload-batch-subfiles) |
| 2026-09-07 23:22 | #16 | discussion | [上传批次补齐 ZIP 子文件关联与逐文件解析状态](UPDATE_LOG.md#2026-09-07-2322-fix-upload-batch-subfiles) |
| 2026-09-07 23:22 | #23 | issue | [上传批次补齐 ZIP 子文件关联与逐文件解析状态](UPDATE_LOG.md#2026-09-07-2322-fix-upload-batch-subfiles) |
| 2026-09-07 23:12 | #1 | discussion | [评分与报价绑定正式 artifact 版本并修复键类型比对](UPDATE_LOG.md#2026-09-07-2312-fix-score-artifact-binding) |
| 2026-09-07 23:12 | #14 | discussion | [评分与报价绑定正式 artifact 版本并修复键类型比对](UPDATE_LOG.md#2026-09-07-2312-fix-score-artifact-binding) |
| 2026-09-07 23:12 | #16 | discussion | [评分与报价绑定正式 artifact 版本并修复键类型比对](UPDATE_LOG.md#2026-09-07-2312-fix-score-artifact-binding) |
| 2026-09-07 23:12 | #22 | issue | [评分与报价绑定正式 artifact 版本并修复键类型比对](UPDATE_LOG.md#2026-09-07-2312-fix-score-artifact-binding) |
| 2026-09-07 23:00 | #13 | discussion | [正式文件逻辑版本链与覆盖历史](UPDATE_LOG.md#2026-09-07-2300-feat-artifact-logical-versions) |
| 2026-09-07 23:00 | #16 | discussion | [正式文件逻辑版本链与覆盖历史](UPDATE_LOG.md#2026-09-07-2300-feat-artifact-logical-versions) |
| 2026-09-07 23:00 | #21 | issue | [正式文件逻辑版本链与覆盖历史](UPDATE_LOG.md#2026-09-07-2300-feat-artifact-logical-versions) |
| 2026-09-07 22:46 | #1 | discussion | [修复长历史补读截断并持久化 pre_chat 消息](UPDATE_LOG.md#2026-09-07-2246-fix-stream-replay-prechat) |
| 2026-09-07 22:46 | #15 | discussion | [修复长历史补读截断并持久化 pre_chat 消息](UPDATE_LOG.md#2026-09-07-2246-fix-stream-replay-prechat) |
| 2026-09-07 22:46 | #20 | issue | [修复长历史补读截断并持久化 pre_chat 消息](UPDATE_LOG.md#2026-09-07-2246-fix-stream-replay-prechat) |
| 2026-09-07 21:57 | #1 | discussion | [补齐聊天消息关联、幂等与异常处理](UPDATE_LOG.md#2026-09-07-2157-fix-agent-chat-contract) |
| 2026-09-07 21:57 | #15 | discussion | [补齐聊天消息关联、幂等与异常处理](UPDATE_LOG.md#2026-09-07-2157-fix-agent-chat-contract) |
| 2026-09-07 21:57 | #19 | issue | [补齐聊天消息关联、幂等与异常处理](UPDATE_LOG.md#2026-09-07-2157-fix-agent-chat-contract) |
| 2026-09-07 16:40 | #18 | issue | [AI 企业资产分类与人工确认闭环](UPDATE_LOG.md#2026-09-07-1640-feat-ai-enterprise-asset-classification) |
| 2026-09-05 20:19 | #1 | discussion | [实现 Office 正式文件远端保存与版本管理](UPDATE_LOG.md#2026-09-05-2019-fix-office-remote-save) |
| 2026-09-05 20:19 | #1 | discussion | [补齐 Agent 聊天消息状态与回复关联契约](UPDATE_LOG.md#2026-09-05-2019-fix-chat-message-contract) |
| 2026-09-05 20:19 | #1 | discussion | [提供上传批次与逐文件解析状态](UPDATE_LOG.md#2026-09-05-2019-fix-upload-batch) |
| 2026-09-05 20:19 | #1 | discussion | [让评审与报价绑定成果版本](UPDATE_LOG.md#2026-09-05-2019-fix-review-quote-version-binding) |
| 2026-09-05 20:19 | #4 | issue | [实现 Office 正式文件远端保存与版本管理](UPDATE_LOG.md#2026-09-05-2019-fix-office-remote-save) |
| 2026-09-05 20:19 | #5 | issue | [补齐 Agent 聊天消息状态与回复关联契约](UPDATE_LOG.md#2026-09-05-2019-fix-chat-message-contract) |
| 2026-09-05 20:19 | #6 | issue | [提供上传批次与逐文件解析状态](UPDATE_LOG.md#2026-09-05-2019-fix-upload-batch) |
| 2026-09-05 20:19 | #7 | issue | [让评审与报价绑定成果版本](UPDATE_LOG.md#2026-09-05-2019-fix-review-quote-version-binding) |
| 2026-09-05 20:19 | #12 | pr | [实现 Office 正式文件远端保存与版本管理](UPDATE_LOG.md#2026-09-05-2019-fix-office-remote-save) |
| 2026-09-05 20:19 | #12 | pr | [补齐 Agent 聊天消息状态与回复关联契约](UPDATE_LOG.md#2026-09-05-2019-fix-chat-message-contract) |
| 2026-09-05 20:19 | #12 | pr | [提供上传批次与逐文件解析状态](UPDATE_LOG.md#2026-09-05-2019-fix-upload-batch) |
| 2026-09-05 20:19 | #12 | pr | [让评审与报价绑定成果版本](UPDATE_LOG.md#2026-09-05-2019-fix-review-quote-version-binding) |
| 2026-09-05 19:55 | #1 | discussion | [打通成果文件 manifest 哈希与版本映射](UPDATE_LOG.md#2026-09-05-1955-fix-artifact-manifest) |
| 2026-09-05 19:55 | #2 | issue | [打通成果文件 manifest 哈希与版本映射](UPDATE_LOG.md#2026-09-05-1955-fix-artifact-manifest) |
| 2026-09-05 19:55 | #11 | pr | [打通成果文件 manifest 哈希与版本映射](UPDATE_LOG.md#2026-09-05-1955-fix-artifact-manifest) |
| 2026-09-05 19:29 | #1 | discussion | [冻结 assembly/artifacts 清单与详情契约](UPDATE_LOG.md#2026-09-05-1929-fix-artifact-list-detail) |
| 2026-09-05 19:29 | #3 | issue | [冻结 assembly/artifacts 清单与详情契约](UPDATE_LOG.md#2026-09-05-1929-fix-artifact-list-detail) |
| 2026-09-05 19:29 | #8 | pr | [冻结 assembly/artifacts 清单与详情契约](UPDATE_LOG.md#2026-09-05-1929-fix-artifact-list-detail) |
