# Octop 功能全景与对齐基线

> 参考对象：`D:\GitHub\HomeMind\src\octop`（Octop / TencentCloud-Octop 的本地副本）
> 用途：作为「功能对齐」的基线清单——先看清对方有什么，再决定做什么。
> 状态标记：✅ 已有且经过测试 ｜ 🟡 部分具备 ｜ ❌ 没有

## 0. 规模对照（先说清量级）

| 维度 | Octop | 本项目 |
|---|---|---|
| 后端 Python 文件 | 657 | 55 |
| 后端代码行 | ~125,000 | ~6,200 |
| API 路由模块 | 55 | 6（routes / tool_routes / auth_routes / run_routes / skill_routes / tenant_routes） |
| 前端 | dashboard（React，含插件渲染宿主）+ desktop + mobile | 6 个 ts/tsx 文件、834 行 |
| 内置插件 | 26 个（air-quality、bilibili-anime、weather、tetris…） | 2 个内建工具（echo / current_time） |
| 测试文件 | 未见统计 | 31 个文件、352 条用例 |

**结论先行**：1:1 对齐 ≈ 20 倍代码量。下面按域拆开，逐项标注我们的位置。

---

## 1. 对话与线程

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 多轮会话、消息持久化 | `infra/history`（17 文件）、`api/routers/agents` | ✅ |
| 会话模式 Ask / Plan / Craft | `infra/agents/conversation_mode.py` | ✅（见 [conversation-modes.md](conversation-modes.md)） |
| 线程版本化历史、轨迹事件 | `infra/history`（"legacy data is never migrated on read"） | 🟡 只有 `runs` / `run_events` |
| 线程分叉 fork | `infra/agents/threads/fork.py` | ❌ |
| 上下文占用分解 | `infra/agents/threads/context_breakdown.py` | 🟡 只有字符预算裁剪 |
| 历史摘要 / 压缩 | harness memory+slim（`infra/agents/memory/slim*.py`） | ❌ 只按整条消息丢弃 |
| 大 payload 旁路（artifact） | `middleware/octop_ui_offload.py` | 🟡 已有设计稿 `features/tool-result-offload.md` |

## 2. Agent 运行时

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| Agent 定义 / 增删改查 | `infra/agents/manager.py`、`api/routers/agents` | ✅ |
| 专家（experts）目录、市场发布、默认 Agent | `infra/agents/experts`（76 文件：catalog / publish / market_creation / avatar…） | ❌ |
| 人设 persona + MBTI 档案 | `infra/agents/persona`、`api/routers/mbti` | ❌ |
| 子代理 subagents | `infra/agents/subagents/catalog.py`、`api/routers/subagents` | ❌ |
| 团队协作（host/member、任务分发） | `infra/agents/teams`（team_manager / jobs / welcome） | ❌ |
| 运行上限 / 运行时参数 | `infra/agents/settings/runtime_limits.py` | 🟡 只有 `max_tool_iterations` |
| 工具目录与默认开关 | `infra/agents/settings/tool_catalog.py` | 🟡 有 registry，无 catalog 概念 |

## 3. 工具、插件与治理

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 工具注册 + JSON Schema 校验 | `infra/agents/plugins`、`api/routers/agent_tools` | ✅ |
| 服务端策略（deny-by-default）+ 审计 | `infra/agents/security/policy_store.py`、`tool_guard_rules.py` | ✅ |
| 插件系统 + 插件市场 + 插件 UI 渲染协议 | `infra/agents/plugins`（33 文件）、`bundled/` 26 个、dashboard `toolRenderers` | ❌ |
| Octop UI 渲染协议（`octop_ui.renderer`） | 26 个内置插件 | ❌ |
| MCP 客户端 | `api/routers/internal_mcp`、`infra/connectors` MCP builders | ✅（stdio、连接池） |
| MCP 服务器管理 / 市场 | `infra/connectors`（45 文件：catalog、credential、oauth） | ❌ 只支持环境变量声明 |
| 隔离执行器（Docker） | 本项目 `rust-executor`（对标物在 Octop 是 workspace/terminal） | 🟡 另一个会话在做 |
| HITL 人工审批 | `infra/gateway/hitl`（store / coordinator / format）、`agents/security/hitl_session` | ✅（见 [hitl.md](hitl.md)） |
| 二进制读取守卫、图片工作区中间件 | `middleware/binary_read_guard.py`、`workspace_image.py` | ❌ |

## 4. 技能

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 技能定义与绑定 Agent | `api/routers/skills`、`infra/skills` | ✅（简版：Skill 表 + skill_names） |
| 技能包 / 安装 / 卸载 | `infra/skills/install.py`、`skill_packages*.py` | ❌ |
| SkillHub 市场（搜索、安装、上传） | `skillhub_market.py`、`skillhub_common.py`、`api/routers/skill_packages` | ❌ |
| 演示文稿等成品技能 | `infra/skills/presentation.py` | ❌ |
| 技能管理技能（skill-manager） | `infra/agents/builtin_skills/skill-manager` | ❌ |

## 5. 知识库（RAG 全链路）

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 知识库 CRUD | `infra/knowledge/service.py`、`api/routers/knowledge_bases` | ❌ |
| 解析 / OCR / 分块 | `parse.py`、`ocr.py`、`chunk.py` | ❌ |
| 向量化 / 索引 / 检索 | `embed.py`、`index.py`、`retrieve.py` | ❌ |
| 引用与来源标注 | `citations.py`、`hint.py` | ❌ |
| 权限门（哪些 Agent 可读） | `gate.py` | ❌ |
| 索引任务队列 | `jobs.py` | ❌ |
| 知识库工具（给模型用） | `tools.py` | ❌ |

## 6. 记忆

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 长期记忆后端 | `infra/agents/memory/backend.py` | ❌ |
| 记忆精简 / 压缩 | `memory/slim.py`、`slim_control.py` | ❌ |
| 记忆可移植（导入导出） | `api/routers/memory_portable.py` | ❌ |

## 7. 模型与推理

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 多提供商适配 | `infra/agents/providers`（14 文件） | 🟡 只有 `OpenAIChatModel` + Mock |
| 提供商探测 / 预设 | `probe.py`、`presets.py`、`resolved.py` | ❌ |
| 模型能力标记 / 推理力度 | `model_flags.py`、`reasoning.py`、`middleware/reasoning.py` | ❌ |
| OAuth 登录式提供商（Codex） | `codex_oauth.py`、`codex_apply.py` | ❌ |
| 本地模型：ONNX 目录/下载/服务 | `onnx_catalog.py`、`onnx_download.py`、`onnx_service.py`、`api/routers/onnx_models` | ❌ |
| Ollama 模型管理 | `api/routers/ollama_models`、`ollama_download_store` | ❌ |

## 8. 网关与多渠道

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 全局交互网关（WS / 流式 / 路由） | `infra/gateway`（55 文件，含 ws、media、process、slash） | 🟡 只有 HTTP + SSE |
| IM 渠道接入（钉钉扫码绑定等） | `gateway/channels`、`api/routers/channels` | ❌ |
| 机器人创建器 | `gateway/bot_creators` | ❌ |
| 斜杠命令 | `gateway/slash`、`api/routers/slash` | ❌ |
| HITL 审批流 | `gateway/hitl` | ✅（见 [hitl.md](hitl.md)） |

## 9. 连接器与实例互联

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 连接器目录 / 凭据 / OAuth | `infra/connectors`（45 文件）、`api/routers/connectors` | ❌ |
| 实例互联（远程 Agent、HTTP 隧道、聊天中转） | `infra/bridge`（10 文件）、`api/routers/bridge` | ❌ |

## 10. 认证、用户与权限

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| OIDC / OAuth / LDAP 登录 | `api/routers/auth_oidc`、`auth_oauth`、`auth_ldap`、`infra/auth`（26 文件） | 🟡 只有 OIDC（API Key + JWT） |
| 用户管理 / 角色 / 邀请 | `api/routers/users`、`user_roles`、`invites`、`infra/users` | 🟡 只有租户成员（subject→role） |
| 首启设置向导 / 密码文件 / TLS | `infra/setup`（15 文件）、`api/routers/setup`、`tls` | 🟡 只有 bootstrap admin key |
| SSO Cookie 会话 | `api/common/sso_cookie.py` | ❌ |

## 11. 运维与可观测

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 可观测（trace / 日志） | `api/routers/observability`、`agents/settings/langfuse.py` | ✅（OpenTelemetry） |
| 用量统计 / 导出 xlsx | `api/common/usage_xlsx.py`、`api/routers/usage` | 🟡 只有日配额计数 |
| 备份与恢复 | `infra/backup`（9 文件）、`api/routers/backup` | ❌ |
| 自更新 | `api/routers/update`、`update_store` | ❌ |
| 存储后端抽象 | `api/routers/storage_backends` | ❌ |
| 环境变量管理 | `api/routers/envs` | 🟡 只有 `.env` |

## 12. 端与多模态

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 语音 STT / TTS | `infra/voice`、`api/routers/voice` | ❌ |
| 媒体生成（图片等） | `api/routers/media_generation`、`agents/settings/media_generation.py` | ❌ |
| 文件上传 / 附件 | `api/routers/uploads`、`api/common/attachments.py` | ❌ |
| 工作区文件系统 / 终端 | `api/routers/workspace`、`filesystem`、`terminal`、`infra/agents/workspace` | 🟡 RCE 执行器（未完成） |
| 远程桌面 | `infra/desktop`、`api/routers/internal` 相关 | ❌ |
| 远程 Android | `infra/mobile`（adb / probe） | ❌ |
| 浏览器环境 | `infra/browser` | ❌ |
| 搜索 | `api/routers/search` | ❌ |
| ACP（IDE 协议） | `api/routers/acp`、`agents/settings/acp.py` | ❌ |

## 13. 自动化与主动能力

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 定时任务 cron（增删改查 + 立即执行） | `infra/cron`（7 文件）、`api/routers/cron` | ✅（见 [cron.md](cron.md)） |
| 主动关怀（proactive care） | `infra/proactive`、`api/routers/proactive_care` | ❌ |
| 渠道推送 | `gateway/channels` | ❌ |

## 14. 平台化杂项

| 能力 | Octop 位置 | 本项目 |
|---|---|---|
| 国际化 i18n | `src/octop/i18n`、`api/routers/i18n` | ❌ |
| CLI（repl + 命令集） | `src/octop/cli`（commands / repl / support） | ❌ |
| OpenAPI 元数据定制 | `api/openapi_meta.py` | 🟡 有 `/openapi.json` 测试 |
| 管理员后台 | `api/routers/admin` | ❌ |
| 偏好设置 | `api/routers/preferences` | ❌ |
| 专家目录 / 技能包安装等其余路由 | 见 §0 的 55 个路由 | ❌ |

---

## 15. 差距统计

| 状态 | 数量 | 说明 |
|---|---|---|
| ✅ 已有 | 15 | 对话、会话模式、Agent CRUD、工具治理、MCP、运行记录、SSE、认证、多租户、限流、评测、OTel、审计链、定时任务 |
| 🟡 部分 | 13 | 技能、模型、上下文、工作区、用户、运维等 |
| ❌ 没有 | 58+ | 知识库、记忆、插件生态、团队、多渠道、多模态、端、i18n… |

## 16. 「功能一样」要分三层看

**第一层：能对齐，且不依赖外部基础设施**（纯代码 + 已有能力）

- 会话模式 Ask / Plan / Craft —— ✅ 已完成（`68287ff`）
- HITL 人工审批（工具调用挂起 → 人工批准 → 继续）—— ✅ 已完成
- 定时任务 cron（进程内调度）—— ✅ 已完成（见 [cron.md](cron.md)）
- 长期记忆（基于现有 DB 的摘要 + 检索）
- 上下文压缩/摘要（替代「整条丢弃」）
- 子代理（同进程内派生子运行）
- 插件化工具协议（不经市场的本地插件目录）
- 用量报表（基于 run / tool_invocations 聚合）

**第二层：能对齐，但需要新基础设施或外部服务**

- 知识库 RAG（需要 embedding 服务或本地向量库）
- 多渠道网关（钉钉/微信/Slack，需要公网回调 + 凭据）
- 语音 STT/TTS（模型文件或云 API）
- 本地模型 ONNX / Ollama（需要模型下载与运行时）
- 对象存储 / 备份恢复 / 自更新（需要外部存储与发布通道）
- 远程桌面 / Android / 浏览器（需要目标设备与网络）
- 连接器市场 / OAuth 集成（需要各家开发者凭据）
- 实例互联 bridge（需要双实例与隧道）

**第三层：不建议照搬**

- 55 个路由的扁平化设计（我们的多租户 + RBAC 是更严格的组织方式）
- 26 个娱乐型内置插件（天气/番剧/tetris）——那是产品内容，不是平台能力
- 专家市场（76 文件）——本质是内容运营体系，不是架构

## 17. 建议的第一批（不依赖外部服务，可独立验收）

| 批次 | 功能 | 依赖 | 预估 |
|---|---|---|---|
| B1 | 会话模式 Ask / Plan / Craft | 无 | 小 · **已完成**（`68287ff`） |
| B1 | HITL 人工审批（挂起/批准/拒绝 + 前端入口） | 无 | 中 · **已完成** |
| B1 | 定时任务 cron（模型 + 调度 + API + 前端） | 无 | 中 · **已完成** |
| B2 | 上下文压缩（摘要替代丢弃） | 模型调用 | 中 |
| B2 | 长期记忆（摘要 + 检索） | 无 | 中 |
| B2 | 子代理（派生运行） | 无 | 中 |
| B3 | 知识库 RAG | embedding 服务 | 大 |
| B3 | 用量报表 | 无 | 小 |

请确认从哪一批开始；B1 的三项都不依赖 Docker / 外部服务，可以像之前那样完整测试后再交付。
