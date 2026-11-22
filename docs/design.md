# CIC × MCP 第三方 Agent 接入实验 · 技术设计（v0.4）

> 状态：第 0、1 步已完成，第 2 步待计划｜日期：2026-11-21｜本文档不提交 git
>
> v0.4 变更：调用第三方改为**异步任务**（MCP tasks 扩展，§6.8）：提交后立即拿到 taskId，结果以推送为主、轮询兜底；同一会话同一时间只有一个未结束的任务；只有 completed 的任务计费，且按 taskId 只结算一次；新增 §13 生产环境（多租户规模）注意事项，实验阶段不实现。
>
> v0.3 变更：补齐全部决策（见 §9 决策记录）；统一使用 `qwen4-max`；1 token = 1 Credit；失败不计费；不兼容旧版 MCP；纯文本任务由 Router 自行处理。
>
> v0.2 变更：支持多 Provider、多 Agent；`conversation_id` 改为必需；Router 改为代码编排（意图识别 → 推荐 → 用户选择 → 路由）；新增入驻时的向量化流程与推荐效果评测。

## 0. 背景与目标

Agent Provider Portal 的 `/agents` 创建流程中，Integration mode 第二种由 **iframe（Unified UI）改为 MCP**。实验要回答三个问题：

1. **入驻校验**：Provider 以 MCP 方式入驻时，需要填写哪些字段？CIC 如何校验其 MCP Server？
2. **用量回传**：沿用 A2A 的 `credit_usage` 概念（只返回本次请求的总消耗 Credit），MCP 下用什么载体回传？
3. **推荐与路由**：多个 Provider、多个 Agent 并存时，如何为用户推荐 Agent？用户选定后，如何在多轮对话中正确续接同一个第三方会话？

实验按完整流程执行，全程用 CLI 演示：**Provider 入驻（测试连接 + 画像向量化）→ 用户对话 → 意图识别 → Agent 推荐 → 用户选择 → 经 MCP 调用第三方 Agent → 回传 credit_usage**。

**非目标**：UI、关系型数据库和中间件（业务数据存内存，向量存本地 Qdrant）、OAuth、流式输出、生产级安全加固。

## 1. 调研结论（影响设计的关键事实）

| 事实 | 影响 |
| --- | --- |
| MCP 最新规范 **2026-07-28** 已改为**无状态**：移除 `initialize` 握手、`Mcp-Session-Id` 和 `ping`；新增 `server/discover`（服务端 MUST 实现） | 入驻校验改为 `server/discover` + `tools/list`；无需单独的 Health Endpoint |
| 跨调用的状态必须通过**显式 handle**（普通 tool 参数）传递 | 第三方 Agent 往往要反问几轮才能开始工作，**`conversation_id` 为必需**（§6.5） |
| 所有 Result 支持 `_meta`，key 由"可选前缀 + 名称"组成；如果写前缀，建议用反向域名格式（如 `com.example/xxx`）；`io.modelcontextprotocol/` 等为保留前缀 | `credit_usage` 放在 `CallToolResult._meta`，与 A2A `Message.metadata` 对位；CIC 统一使用前缀 `com.cic/`（§5.1） |
| 鉴权：HTTP 传输 SHOULD 遵循 OAuth 2.1，但规范明确允许"自定义鉴权策略" | 首期用 API Key（`Authorization: Bearer <key>`），后续可平滑升级 OAuth |
| 传输：只推荐 Streamable HTTP；HTTP+SSE 已 Deprecated | 入驻只支持 Streamable HTTP（远程 HTTPS） |
| `mcp` Python SDK **2.3.0**（`MCPServer` / `Client`）；`agentscope` **2.0.12**（与 1.x 不兼容，内置 `MCPClient`，依赖 `mcp>=2.0`） | 技术选型基线 |
| AgentScope `MCPClient` 会把 MCP 结果转换为 ToolResponse，文档未说明是否保留 `_meta` | CIC 侧不使用 AgentScope `MCPClient`，改为自行封装 `mcp` SDK `Client` |
| 官方扩展 **`io.modelcontextprotocol/tasks`**：`tools/call` 可以立即返回 `CreateTaskResult`（`resultType: "task"`、`taskId`、`status: working`、`pollIntervalMs`、`ttlMs`），之后用 `tasks/get` 轮询、`tasks/cancel` 取消；任务状态有 `working / input_required / completed / failed / cancelled`，`completed` 的 `result` 就是原本的 `CallToolResult`。是否创建任务由**服务端逐次决定**，客户端声明了该能力就必须两种返回都能处理 | 调用第三方改为异步任务，不再同步等待结果（§6.8） |
| 任务状态推送：客户端用 `subscriptions/listen` 订阅指定的 `taskIds`，服务端用 `notifications/tasks` 推送，内容与 `tasks/get` 相同（完成时带结果）。推送是服务端**可选**能力；订阅的过滤条件在打开连接时固定，**不能追加 taskId**；服务端不保证订阅时补发当前状态 | 推送为主、轮询兜底；订阅后补查一次（§6.8） |
| 任务自身的 `input_required` 表示"服务端需要客户端回答一个结构化请求"（MRTR，如 elicitation 表单），通过 `tasks/update` 回答 | 与我们的开放式反问不是一回事。反问仍放在任务结果的 `structuredContent.status` 里，每轮用户输入对应一个任务 |
| Python `mcp` 2.3.0 两端都实现了 `subscriptions/listen` 和扩展框架（服务端 `Extension`：注册新方法、拦截 `tools/call`；客户端 `ClientExtension`：识别扩展的返回形态和通知），但**没有现成的 tasks 实现**；官方参考实现只有 TypeScript 版 | 实验中两端的 tasks 都要自己实现；这也代表 Python 第三方接入时的真实成本 |
| 百炼 `text-embedding-v4`（Qwen3-Embedding）可走 OpenAI 兼容 `/embeddings` 接口，维度可选 64～2048，默认 1024；稀疏向量只能用 DashScope 原生接口 | Embedding 选型；BM25 / 稀疏向量暂不做（§6.4） |
| 百炼 `qwen3-rerank` 单次最多 500 个文档，支持 OpenAI 兼容调用；`gte-rerank` 已于 2026-05-30 下线 | 可选的 Rerank 精排 |
| 百炼 `qwen4-max`：旗舰多模态模型，输入支持文本、图像、视频；支持工具调用和结构化输出；兼容 OpenAI API | Router 与所有第三方 Agent 统一使用 `qwen4-max` |
| 阿里云 OSS 推荐使用 Python SDK V2（`alibabacloud-oss-v2`），支持预签名 URL | 图片上传与公网访问；实验 Bucket 为公共读 |

## 2. 总体架构

### 2.1 入驻链路（模拟 Provider Portal 提交）

```
[cic-onboard <agent_id>]
  ① 测试连接：server/discover → 鉴权生效检查 → tools/list → 入口 Tool 契约校验          (§4.2 / §4.3)
  ② 构建 Agent 画像：名称 + 分类 + 标签 + 描述 → 1 段画像文本；示例问题逐条作为独立文本   (§4.5)
  ③ Embedding：text-embedding-v4
  ④ 写入本地 Qdrant：每个 Agent 1 个画像向量 + N 个示例问题向量；payload 带 agent_id、状态、是否支持图片
```

### 2.2 对话链路（Router 代码编排）

```
用户输入（文本 + 可选图片 → OSS 公网 URL）
  │
  ▼
[意图识别工具]  LLM 结构化输出；输入 = 近期对话 + 第三方会话列表 + 待选候选 + 本次输入（每轮都经过，第一版不设快捷选择）
  ├─ supplement(T1)     → 带 T1 的 conversation_id 向同一 Agent 提交任务 → 推送/轮询拿到结果后直接展示 Agent 回复
  ├─ select_candidate   → 用户从推荐候选中做出选择（如"1"、"用第一个"、"旅行规划那个"）→ 调用所选 Agent（开新会话，转发待处理任务）
  ├─ router_answer      → Router LLM 直接回复
  ├─ recommend(query)   → [Embedding 工具] → [向量检索工具] → 按 Agent 聚合 → (可选 Rerank) → 阈值过滤 → top_k
  │                         ├─ 有候选 → 按匹配度倒序展示，进入"等待选择"
  │                         └─ 无候选 → 告知"暂无合适的 Agent"，由 Router 尽力回答
  └─ clarify            → Router 向用户澄清（例如无法判断用户在回答哪个会话）
```

### 2.3 部署拓扑

```
┌──────────────────────────────── CIC 侧 ─────────────────────────────────┐
│ cic-chat（Router 编排 + MCP Client Pool + 任务监听 + 账本）              │
│ cic-onboard / cic-eval                                                   │
│ 本地 Qdrant（./data/qdrant）  Agent 入驻数据（./data/agents.yaml）        │
└────────────│──────────────────────────────────│────────────────────────┘
             │ tools/call → taskId                │ (Bearer key B)
             │ subscriptions/listen ⇠ 推送        │
             │ tasks/get（兜底轮询）(Bearer key A) │
┌────────────▼────── Provider A ───────┐  ┌───────▼────── Provider B ──────────┐
│ MCP Server :8001                     │  │ MCP Server :8002                   │
│  ├─ trip_planner  → Agent A1         │  │  └─ weekly_report → Agent B1       │
│  └─ product_copy  → Agent A2         │  │                                    │
└──────────────────────────────────────┘  └────────────────────────────────────┘
```

| Agent | 能力（用于验证反问 + 多模态） |
| --- | --- |
| A1 `trip_planner` 旅行规划 | 先反问目的地、日期、预算，信息齐全后输出行程 |
| A2 `product_copy` 商品文案 | 看商品图片写文案，先反问投放平台与风格 |
| B1 `weekly_report` 周报撰写 | 先反问本周工作内容，再生成周报 |

另有约 12 个**仅画像、不接 MCP** 的模拟 Agent，其中包含与上面 3 个功能相近、容易混淆的 Agent（如酒店预订、签证咨询、会议纪要、海报设计），专门用于推荐评测（§7）。`cic-chat` 默认只推荐可调用的 Agent，加 `--include-mock` 后才会混入模拟 Agent。

## 3. 第三方 Provider 侧

### 3.1 第三方 Agent（`provider/agent.py`）

- AgentScope 2.0 `Agent`（ReAct 循环），模型统一使用 `qwen4-max`，走百炼 OpenAI 兼容接口（`base_url` + `api_key`）。
- 输入：`message: str` + `image_urls: list[str]`，组装为 AgentScope 多模态消息（Text + Image URL）。图片 URL **只接受 `https://`**：AgentScope 会把 `file://` 地址读成本地文件发给模型，必须拒绝。
- 记忆：每个 `conversation_id` 对应一个 Agent 实例，同一会话的多轮输入共享记忆，第三方侧上下文因此能串联起来。某一轮失败时回滚这一轮写入的记忆，用户可用同一个 `conversation_id` 重试；同一会话的多次调用按顺序执行。
- 反问能力：信息不足时 Agent 不硬做，而是向用户提问。使用结构化输出 `{status, reply}`：`status=input_required` 表示反问，`status=completed` 表示本轮已交付。
- 用量统计：累计本次请求内所有模型调用的 `input_tokens + output_tokens`。
- Credit 换算（Mock 规则）：**1 token = 1 Credit**，即 `credit_usage = total_tokens`。

### 3.2 MCP Server（`provider/server.py`）

- `mcp` SDK `MCPServer`，Streamable HTTP，路径 `/mcp`。启动参数指定 Provider，从 `provider/catalog.py` 加载该 Provider 名下的 Agent 定义（人设 Prompt、Tool 名、描述）。
- **鉴权**：ASGI 中间件校验 `Authorization: Bearer <key>`；合法 key 来自 `.env`，每个 Provider 不同；校验失败返回 `401` + `WWW-Authenticate: Bearer`。
- **tasks 扩展**（§6.8）：在 `server/discover` 的 `capabilities.extensions` 中声明 `io.modelcontextprotocol/tasks`；实现 `tasks/get`、`tasks/cancel`，以及 `subscriptions/listen` 中的 `taskIds` 订阅与 `notifications/tasks` 推送。任务状态存在内存中，按 `ttlMs` 过期清理。模拟 Provider 对入口 Tool **统一返回任务**；启动参数 `--delay <秒>` 用于模拟长任务，`--no-push` 用于模拟不支持推送的 Provider。
- **Tool**：**每个 Agent 对应 1 个 Tool**，所有 Tool 都遵循 CIC 入口 Tool 契约（§4.3）。以 `trip_planner` 为例：

```json
{
  "name": "trip_planner",
  "description": "（Provider 写的能力描述）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "message": { "type": "string" },
      "image_urls": { "type": "array", "items": { "type": "string", "format": "uri" } },
      "conversation_id": { "type": "string", "description": "开启新会话时省略；继续会话时传入上次返回的值" }
    },
    "required": ["message"]
  }
}
```

- **提交后立即返回任务**：

```json
{
  "resultType": "task",
  "taskId": "786512e2-9e0d-44bd-8f29-789f320fe840",
  "status": "working",
  "createdAt": "2026-11-21T10:30:00Z",
  "lastUpdatedAt": "2026-11-21T10:30:00Z",
  "ttlMs": 3600000,
  "pollIntervalMs": 2000
}
```

- **任务完成后的结果**（推送 `notifications/tasks` 或 `tasks/get` 拿到的内容相同；`result` 就是原本的 `CallToolResult`，以反问为例）：

```json
{
  "taskId": "786512e2-9e0d-44bd-8f29-789f320fe840",
  "status": "completed",
  "result": {
    "content": [{ "type": "text", "text": "请问出发日期和预算大概是多少？" }],
    "structuredContent": {
      "conversation_id": "conv_7f3a…",
      "status": "input_required",
      "reply": "请问出发日期和预算大概是多少？"
    },
    "isError": false,
    "_meta": { "com.cic/credit_usage": 812 }
  }
}
```

- 不走任务的 Provider 可以像原来一样，直接在 `tools/call` 中返回上面 `result` 这部分（`resultType: "complete"`），CIC 两种都处理。

- `status=completed` 时结构相同，`reply` 为任务结果。每一轮（每个任务）都返回**该轮**的 `credit_usage`，反问轮也一样。
- 处理失败时（包括遇到未知或过期的 `conversation_id`），Provider **直接抛出异常即可**，无需携带 `credit_usage`；走任务时，任务进入 `failed`。CIC 按 §5.2 判定为失败，不计费。

## 4. 核心问题一：MCP 入驻

### 4.1 字段对比（A2A 现状 vs MCP 建议）

| 字段 | A2A（现状） | MCP（建议） | 说明 |
| --- | --- | --- | --- |
| 协议版本 | A2A 1.0（只读） | MCP `2026-07-28`（只读） | **不兼容旧版**（如 `2025-11-25`）MCP Server |
| 发现入口 | Agent Card URL | —（删除） | 由 Endpoint 上的 `server/discover` 取代 |
| 服务端点 | A2A Endpoint | **MCP Server URL**（HTTPS，Streamable HTTP） | 例：`https://agent.example.com/mcp` |
| 健康检查 | Health Endpoint | —（删除） | `ping` 已从规范中移除；探活改用 `server/discover` |
| 传输协议 | JSON-RPC / HTTP+JSON / GRPC（测试后选择） | 固定 Streamable HTTP | 不支持 stdio 和已弃用的 HTTP+SSE |
| 鉴权方式 | —（隐含 Key） | **鉴权方式**：首期仅 `API Key` | 预留 OAuth 2.1 选项 |
| 凭证 | Provider Agent Key | **API Key** | 固定以 `Authorization: Bearer <key>` 发送 |
| 能力展示 | Skills（来自 Agent Card） | **Tools**（来自 `tools/list`） | 展示 name / title / description / inputSchema |
| 入口 Tool | — | **入口 Tool（单选）** | **1 个 Agent = 1 个入口 Tool**。同一 Provider 的多个 Agent 可以共用同一 MCP Server URL，各选不同的 Tool；同一组 `(URL, Tool)` 不可被两个 Agent 重复登记 |
| 示例问题 | — | **新增：示例问题（3～10 条）** | 写"用户会怎么提问"，用于推荐召回（§4.5）。A2A Agent 也适用，可复用 A2A `AgentSkill.examples` |
| 成本单价 | HKD / Credit | HKD / Credit（同 A2A） | MCP 也按 credit_usage 结算 |

名称、分类、标签、描述这几项是 Portal 现有的基础字段，不变，但都会参与画像向量化（§4.5）。

### 4.2 "测试连接"步骤（`cic-probe` 即为其原型）

1. **Discover**：`POST server/discover`，要求返回 2xx，支持的版本中包含 `2026-07-28`，且 `capabilities.tools` 存在。旧版 Server 没有 `server/discover`，在这一步直接判定为不通过；CIC Client 固定使用 `2026-07-28`，不向下协商。同时记录 Server 是否声明了 `io.modelcontextprotocol/tasks` 扩展（仅展示，不作为通过条件）。
2. **鉴权生效检查**：不带 key 调用 `tools/list`，应返回 `401`。若返回 200，**直接阻断提交**，并在 UI 上报错"MCP Server 未启用鉴权"。
3. **带 key 列出 Tools**：至少 1 个 tool；校验 name 字符集（`A-Za-z0-9_-.`），以及 `inputSchema` 是否为合法 JSON Schema。
4. **选择入口 Tool 并校验契约**：Provider 为该 Agent 单选一个 Tool，CIC 校验其 `inputSchema` 符合 §4.3 契约。
5. **记录测试结果**：`testedAt` + `fingerprint(url, sha256(key), selectedTool)`，沿用 A2A 的 15 分钟有效期规则。

> 真实 `tools/call` 返回的 `status`、`conversation_id`、`credit_usage` 是否正确，在**沙箱审核**阶段验证（与 A2A 一致），不放在测试连接里，以免产生真实消耗。

### 4.3 CIC 入口 Tool 契约

CIC 不是通用的 MCP Host，而是带计费和多轮会话的 Marketplace，所以要在 MCP 之上约定一层固定契约。代价是：Provider 现有的任意 MCP Server 不能直接接入，需要包装出一个符合契约的入口 Tool。

| 位置 | 字段 | 要求 |
| --- | --- | --- |
| `inputSchema` | `message` | string，**必填** |
| `inputSchema` | `conversation_id` | string，可选；继续会话时传入 |
| `inputSchema` | `image_urls` | string[]，可选；**声明了才视为支持图片**，否则 Router 不传图片，推荐时也会过滤掉该 Agent。CIC 只会传入 `https://` 地址 |
| `inputSchema` | 其他参数 | 只允许可选参数；CIC 不会填写 |

> 校验 `inputSchema` 时，可选参数要同时接受 `{"type": "string"}` 和可空写法 `{"anyOf": [{"type": "string"}, {"type": "null"}]}`：pydantic 等常见框架会把 `str | None` 生成后一种（实验中的模拟 Provider 即如此）。
| 返回形态 | — | 二选一：直接返回 `CallToolResult`；或返回 `CreateTaskResult`（`resultType: "task"`，§6.8），任务 `completed` 时的 `result` 为下面各行描述的 `CallToolResult`。CIC 两种都处理，**长任务建议走任务** |
| 任务 | `ttlMs` | 走任务时，**必须 ≥ 3600000（1 小时）或为 `null`**（不限时），给 CIC 留出足够的取结果时间。不达标时记录契约违规，实验阶段不拦截 |
| `structuredContent` | `conversation_id` | string，必填；新会话由 Provider 签发 |
| `structuredContent` | `status` | `input_required`（反问用户）\| `completed`（本轮已交付） |
| `structuredContent` | `reply` | string，必填；面向用户的回复 |
| `content` | text | 至少 1 个 text block，内容与 `reply` 一致（规范的向后兼容要求） |
| `_meta` | `com.cic/credit_usage` | number ≥ 0；**成功结果必填**，失败时不要求（§5） |

**CIC 对 Provider 的保证**：同一个 `conversation_id` 同一时间**最多只有一个未结束的调用或任务**。CIC 在上一轮进入终态（返回结果，或任务 completed / failed / cancelled）之前，不会提交下一轮。因此 Provider 无需处理同一会话的并发调用；如果仍然收到，可以直接返回错误。

### 4.4 CIC 调用时随请求下发的信息（建议）

在 `tools/call` 的 `params._meta` 中携带：

- `com.cic/request_id`：CIC 计量关联 ID，便于双方对账；
- `traceparent`：OpenTelemetry 链路（2026-07-28 规范已有约定）。

### 4.5 Agent 画像与向量化（提交时执行）

**画像文本**（每个 Agent 生成 1 段）：

```
名称：旅行规划
分类：出行, 生活服务
标签：行程规划, 自由行, 预算
描述：根据目的地、出行日期和预算，为用户规划逐日行程……
```

**写入 Qdrant 的点**（多向量：每个 Agent 有 1 + N 个点）：

| kind | 文本 | 数量 |
| --- | --- | --- |
| `profile` | 画像文本 | 1 |
| `example` | 单条示例问题，如"帮我规划一个 5 天的日本自由行" | 3～10 |

payload：`agent_id, provider_id, kind, text, status, supports_image, callable, embedding_model, dim`。

- 只有 `status=published` 的点参与检索。实验中由 `cic-onboard` 直接写入 `published`，模拟审核已通过。
- Agent 编辑后重新提交时，先删除该 `agent_id` 的全部点，再重新写入。
- `supports_image` 取自入口 Tool 的 `inputSchema` 是否声明了 `image_urls`。
- **生产落地**：使用 pgvector，表结构 `(agent_id, kind, text, embedding vector(1024), …)`。实验代码通过 `VectorStore` 接口隔离，切换存储只需替换实现。

## 5. 核心问题二：credit_usage 回传方案

| 方案 | 载体 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A（推荐）** | `CallToolResult._meta["com.cic/credit_usage"]` | 规范内扩展点；不进入 LLM 上下文；与 A2A `Message.metadata` 对位；SDK 原生支持 | 通用 MCP 框架可能丢弃 `_meta`，CIC 需自行读取（本实验自封装 Client，已覆盖） |
| B | `structuredContent.credit_usage` + `outputSchema` | 结构化、可校验 | 与业务数据混在一起；会进入 LLM 上下文，可能被模型复述 |
| C | 进度通知 / 自定义 notification | 可以中途上报 | 需要 SSE；进度通知不保证送达，不适合用于结算 |
| D | HTTP 响应头 | 不用改 body | 走 SSE 时响应头先于结果发出，带不了最终用量 |

**规则（推荐方案 A，与 A2A 对齐）**：

1. 只在最终的 `CallToolResult` 中返回**本轮的实际总消耗**；进度通知不携带用量。
2. 值为 JSON number，≥ 0；`0` 是合法值。
3. 缺失、`null` 或空字符串时，CIC 视为结算失败，本次调用以 Error 终止。
4. **调用失败不计费**，Provider 失败时也无需返回 `credit_usage`。失败由 CIC 代码根据协议信号判定（§5.2）。
5. 走任务时，用量放在 `completed` 任务的 `result._meta`（即最终 `CallToolResult` 的 `_meta`）。**只有 `completed` 的任务计费**，`failed`、`cancelled` 和过期的任务不计费。
6. 推送和轮询可能先后拿到同一个任务的完成结果，CIC **按 taskId 只结算一次**。

### 5.1 `_meta` key 统一使用 `com.cic/` 前缀

CIC 在 MCP `_meta` 中约定的所有 key 都统一加 `com.cic/` 前缀，例如 `com.cic/credit_usage`、`com.cic/request_id`。

**为什么需要前缀**（MCP 规范中前缀是可选的，不加也合规，加前缀是 CIC 的主动约定）：

1. **`_meta` 是多方共用的命名空间。** MCP 协议本身会在每个结果的 `_meta` 中写入 `io.modelcontextprotocol/serverInfo` 等字段；SDK、网关、链路追踪（`traceparent`）也会使用 `_meta`。不加前缀的 key 没有归属，容易与其他参与方撞名。
2. **第三方 MCP Server 往往不只接入 CIC 一个平台。** 其他平台也可能约定一个叫 `credit_usage` 的字段，但单位是 token、美元或别家的积分。加上 `com.cic/` 前缀，就明确了"这是 CIC Credit，按 CIC 的规则结算"，Provider 同时对接多个平台时也不会混淆。
3. **为规范演进留出余地。** 将来 MCP 如果统一规定用量或计费字段，不带前缀的名字可能与之冲突，而带前缀的 key 不受影响。
4. **符合规范的命名建议。** 规范建议前缀使用反向域名格式（`com.example/`），并保留了 `io.modelcontextprotocol/`、`dev.mcp/` 等前缀；`com.cic/` 符合格式，也不在保留范围内。

**说明**：

- 前缀只是命名约定，不做 DNS 校验，所以不需要与公司的实际域名一致。
- 前缀一经对外发布，就不应再修改，否则所有已接入的 Provider 都要跟着改。
- A2A 侧仍保持 `Message.metadata.credit_usage`，不加前缀：Portal 的 A2A 接入文档已经按这个字段名对外说明，保持不变可避免影响已接入的 Provider。两种接入方式在文档中分别说明即可。

### 5.2 调用失败的判定（不计费）

Provider 失败时直接抛出异常即可，不必返回 `credit_usage`。是否失败由 **CIC 代码**根据协议信号判定，**不交给 LLM**：这个判断直接决定是否计费，而 LLM 读一段文字时，很难稳定区分报错和正常回复（例如"抱歉，暂时无法处理"）。

| 失败情况 | CIC 看到的信号 | 处理 |
| --- | --- | --- |
| 网络不通 / 超时（提交任务或直接调用时） | 连接异常、超时 | 失败，不计费 |
| Key 失效 | HTTP 401 / 403 | 失败，不计费，并告警 |
| 服务端崩溃 | HTTP 5xx，或 JSON-RPC error 响应 | 失败，不计费 |
| Agent 代码抛异常 | `CallToolResult.isError: true`（SDK 默认会把 tool 抛出的异常包装成这种结果，Provider 无需自己写） | 失败，不计费 |
| 任务失败 / 被取消 | 任务状态 `failed` / `cancelled` | 失败，不计费 |
| 任务过期或丢失 | 超过 `createdAt + ttlMs` 仍未结束，或 `tasks/get` 返回"任务不存在" | 失败，不计费 |
| 返回了结果但不合契约 | 缺少 `conversation_id` / `status` / `reply`，或缺少 `com.cic/credit_usage` | 失败，不计费，并记录契约违规 |

注意：按 tasks 扩展规范，Agent 代码抛异常时任务通常仍是 `completed`，只是 `result.isError: true`；两种情况 CIC 都判定为失败。

失败之后如何回应用户，由 Router 决定（§6.5）。

## 6. Router（代码编排）

Router 由**代码固定编排**流程：每轮先做意图识别，再按结果分支。LLM 只负责意图识别和 Router 自己的回复，**向量只在代码中流转**，不进入 LLM。

### 6.1 会话状态（内存，`cic/session.py`）

```python
Session(
    history=[...],                 # 完整对话：用户、Router、第三方 Agent 的发言（Agent 回复也记入，Router 因此拥有完整上下文）
    threads=[                      # 第三方会话列表；status: running | input_required | completed | failed
        Thread(ref="T1", agent_id="trip_planner", conversation_id="conv_7f3a…",
               status="input_required", last_reply="请问出发日期和预算？", updated_turn=3,
               task_id=None),      # running 时记录进行中的 taskId
    ],
    pending=PendingSelection(      # 等待用户选择时才有值
        message="帮我规划去东京的旅行", image_urls=[], candidates=[...]),
)
```

### 6.2 每轮流程

1. 每轮输入都先调用**意图识别工具**（§6.3）。第一版**不做快捷选择**：即使用户只输入编号，也要经过意图识别。按 intent 分支：
   - `supplement` → 带原 `conversation_id` 向同一 Agent 提交新一轮任务（§6.5、§6.8）；
   - `select_candidate` → 代码校验 `candidate_index` 落在 `pending.candidates` 范围内，然后调用所选 Agent，开启新会话；
   - `router_answer` → Router LLM 直接回复；
   - `recommend` → Agent 推荐（§6.4）→ 展示候选并写入 `pending`；
   - `clarify` → Router 向用户澄清。
2. **Agent 只能从 Router 推荐的候选中选择**。用户点名了候选之外的 Agent 时，不直接调用，而是按新需求重新推荐。
3. 只要用户没有选择候选，而是输入了别的内容，就清空 `pending`。
4. 某个会话处于 `running`（任务尚未结束）时，意图识别仍照常进行；如果判定为对这个会话的 `supplement`，Router **不提交**，直接回复"××Agent 正在处理上一条消息，请稍候"。用户可以在等待期间与 Router 聊天，或开启其他任务。
5. 选定 Agent 后，转发给 Agent 的 `message` 是**用户的原话**（即 `pending.message`），图片一并转发，**不携带上下文**，也不经 LLM 改写（已确认）。理由如下：
   - 如果 Agent 需要更多信息，它会返回 `input_required`，后续补充沿用同一个 `conversation_id`，上下文由第三方 Agent 自己串联；
   - 针对已有对话内容的纯文本处理（如"把上面那段翻译成英文"）由 Router 自己完成（§6.3），不推荐 Agent，因此不存在"Agent 拿不到上文"的问题。

### 6.3 意图识别工具（`cic/intent.py`）

输入：近期对话、第三方会话列表（ref、Agent、status、Agent 最后一次提问）、待选候选、本次输入、是否附带图片。

输出（结构化）：

```json
{
  "intent": "supplement | select_candidate | router_answer | recommend | clarify",
  "thread_ref": "T1",
  "candidate_index": 1,
  "search_query": "规划日本东京自由行行程",
  "reason": "用户在回答 T1 关于日期和预算的提问"
}
```

判定规则（写进 Prompt）：

| intent | 何时使用 |
| --- | --- |
| `supplement` | 用户在回答某个 `input_required` 会话的提问；或要求对某个 `completed` 会话的结果做**需要该 Agent 专业能力**的追问、修改（如"第二天的行程换成购物"） |
| `select_candidate` | 处于"等待选择"状态，用户从推荐候选中做出了选择（编号、序数或 Agent 名称均可）；输出 `candidate_index` |
| `router_answer` | 问候、闲聊、简单常识、关于平台本身的问题；以及 **LLM 本身就能完成的纯文本任务**，如对已有对话内容（包括第三方 Agent 的回复）做翻译、总结、润色、改格式。Router 拥有完整上下文，不需要额外工具 |
| `recommend` | 需要专业能力或需要产出成果的**新**任务。即使目标 Agent 类型与已有会话相同，只要是新任务，也重新推荐 |
| `clarify` | 有多个会话都可能是补充对象而无法确定；或需求本身不清晰 |

`search_query` 是 LLM 把本次需求改写成的**一句独立任务描述**（已补全上下文中的指代），只用于检索，不转发给 Agent。

> **第一版不使用查询改写（决策 35）**：评测中 V4（改写后检索）的效果反而下降（§7.3），所以第一版推荐时直接用**用户原话**检索。`search_query` 仍由意图识别输出，只用于日志展示，上线后结合真实数据再决定是否启用。

### 6.4 Agent 推荐（Embedding 工具 + 向量检索工具）

| 工具 | 职责 |
| --- | --- |
| Embedding 工具（`recommend/embedding.py`） | `embed(texts) -> vectors`，调用 text-embedding-v4（1024 维）；入驻和检索共用 |
| 向量检索工具（`recommend/vector_store.py`） | `search(vector, top_n, filters) -> points`；`VectorStore` 接口 + Qdrant 本地实现 |
| Rerank（`recommend/rerank.py`，开关控制） | `qwen3-rerank` 对召回的 Agent 精排 |
| 推荐器（`recommend/recommender.py`） | 把以上步骤串起来 |

推荐器流程：

1. 对 `search_query` 做 Embedding。
2. 按 `agent_id` 分组召回 top 20 个 **Agent**（Qdrant `query_points_groups`，每组只取得分最高的点；按点召回时，示例问题多的 Agent 会挤占名额）。过滤条件：`status=published`，附带图片时再加 `supports_image=true`。
3. 每个 Agent 取得分最高的点作为匹配分，并记下命中的是哪条文本，作为"匹配依据"；再**回查注册表**，剔除已下架或不存在的 Agent（索引可能落后于注册表，§13.10）。
4. （`RERANK_ENABLED=true` 时）对召回的 Agent 精排：query 为 `search_query`，document 为画像文本 + 示例问题。
5. 过滤掉低于阈值的 Agent。向量分数与 Rerank 分数各有一个阈值，取值由评测（§7）确定。
6. 按分数倒序取 `RECOMMEND_TOP_K` 个（实验取 2，生产取 5）。

CLI 展示：

```
为你找到以下 Agent（按匹配度排序）：
  [1] 旅行规划 · Provider A   匹配度 0.82   依据："帮我规划一个 5 天的日本自由行"
  [2] 酒店预订 · Provider X   匹配度 0.61   依据："东京有哪些性价比高的酒店"
回复编号或 Agent 名称进行选择，或直接输入其他内容
```

**不做 BM25 / 稀疏向量**：Agent 描述和用户问题都是自然语言，量也不大，先只用向量。如果评测发现专有名词类的问题经常推荐错，再加稀疏向量。

### 6.5 调用第三方 Agent 与多轮会话

- **conversation_id 由 Provider 签发（已确认）**：新会话不传，由 Provider 在 `structuredContent.conversation_id` 返回，后续调用再传入。这是 2026-07-28 "Stateful Tools" 指南推荐的方式，Provider 也可以把 id 与 api_key 绑定以防越权。
- **用 `structuredContent.status` 表达反问（已确认）**：第三方 Agent 每次回复都要说明这次属于哪种情况：`input_required` 表示在向用户提问、等待补充，任务尚未完成；`completed` 表示已交付结果。这与 A2A 的 `TASK_STATE_INPUT_REQUIRED` 语义对位。主要用途：
  - 帮助意图识别判断用户输入是"补充上一轮"还是"新需求"：会话处于 `input_required` 时，用户的下一句话大概率是补充。没有这个字段，Router 只能根据回复文字猜测，例如行程结尾一句"需要我再调整吗？"会被误判为反问；
  - UI 可以显示"等待补充"标记；
  - 每次调用都正常结束、独立结算，用户可以在反问期间闲聊或切换任务。

  MCP MRTR（`InputRequiredResult` + elicitation）面向结构化表单式的一次性补充，不适合开放式多轮对话，因此不采用，可留作以后"付费确认"一类场景使用。
- Router 维护 `T1/T2 ↔ (agent_id, conversation_id)` 映射，LLM 只引用 `T1` 这类短别名，接触不到真实 id。
- **Agent 回复直接展示（已确认）**，并标注来源（`[旅行规划 · Provider A] T1 · 等待补充`），Router 不改写，以保证忠实，也省一次模型调用。
- **调用失败**：失败由代码按 §5.2 判定，不计费。之后**如何回应用户由 Router 决定**：告知"该 Agent 暂时不可用"，让用户选择重试或重新推荐其他 Agent。对应的会话标记为 `failed`；用户选择重试时，沿用原 `conversation_id` 再提交一次。CIC **不会自动重放**，以免重复执行和重复计费。
  - **会话持久化**：重试能成功，前提是 Provider 仍保有该会话。实验中的模拟 Provider 只在内存中保存会话，重启后 `conversation_id` 失效，重试会得到 `isError`（会话不存在），CIC 同样判定为失败、不计费。生产环境的接入契约应要求 Provider 持久化会话；无法续接时明确报错，以便 Router 引导用户「重新推荐」或重新开始。
- **每轮一个任务**：每一轮用户输入向第三方提交一次 `tools/call`，对应一个任务（或一次直接返回）。同一会话在上一轮结束前不提交下一轮（§4.3 的保证）。

### 6.6 账本与 CLI 展示（`cic/ledger.py`、`cic/cli.py`）

- 每次调用记录：`{turn, provider_id, agent_id, thread_ref, conversation_id, status, credit_usage, price_per_credit, hkd}`。
- 每轮打印明细；`/usage` 按 Provider → Agent 汇总；`/threads` 查看会话列表（含 `running` 中的任务）；`/image <path>` 附加图片；`/new` 新会话；`/exit` 退出。
- 提交任务后 CLI **不阻塞**：先打印"已提交，处理中"，用户可以继续输入；任务结束时异步打印 Agent 回复和 credit。
- 图片上传：通过 `alibabacloud-oss-v2` 上传到 `cic-mcp-lab/{yyyyMMdd}/{uuid}.{ext}`。Bucket 为**公共读**，直接使用对象的公网 URL（`https://{bucket}.{endpoint}/{key}`），不使用签名 URL。

```
[intent] supplement → T1   (用户在回答 T1 的提问)
[旅行规划 · Provider A] T1 · completed
  ……行程内容……
[credit] trip_planner credit_usage=2431 ≈ HKD 0.2431   (本会话累计 3243 Credit)
```

### 6.7 验收场景

| # | 前置状态 | 用户输入 | 期望行为 |
| --- | --- | --- | --- |
| 1 | 无 | 你好 | `router_answer`，不推荐 |
| 2 | 无 | 帮我规划去东京的旅行 | `recommend` → 展示 2 个候选，旅行规划排第 1 |
| 3 | 等待选择 | 用第一个 | `select_candidate`（index=1）→ 调用 `trip_planner`（新会话）→ 新建 T1，展示反问 |
| 4 | T1 等待补充 | 12 月 5 号出发，预算 1 万 | `supplement T1`，Provider 侧上下文连续 |
| 5 | T1 等待补充 | 今天星期几？ | `router_answer`，T1 状态不变 |
| 6 | T1 等待补充 | 另外帮我写本周周报 | `recommend` → 选择 → 新建 T2（另一个 Provider） |
| 7 | T1、T2 均等待补充 | 预算改成 2 万 | `supplement T1`（从语义可判断归属） |
| 8 | T1、T2 均等待补充 | 好的，就这样 | `clarify`（无法判断补充的是哪个会话） |
| 9 | T1 已完成 | 第二天的行程换成购物 | `supplement T1`（对结果的修改） |
| 10 | T1 已完成 | 再帮我规划一次大阪旅行 | `recommend`（新任务）→ 选择后新建 T3 |
| 11 | 无 | [图片] 帮这个商品写小红书文案 | `recommend`，只推荐支持图片的 Agent → 选择后带 `image_urls` 调用 |
| 12 | 无 | 帮我订一张明天去上海的机票 | `recommend` → 无 Agent 超过阈值 → 提示"暂无合适的 Agent" |
| 13 | 等待选择 | 算了，讲个笑话吧 | `router_answer`，清空待选候选 |
| 14 | T2 等待补充，Provider B 已停止 | 周报补充内容 | `supplement T2` → 连接失败，判定失败、不计费 → Router 告知不可用，提供"重试 / 重新推荐"两个选项 |
| 15 | T1 已完成 | 把上面的行程翻译成英文 | `router_answer`：Router 基于完整上下文直接翻译，不调用 Agent、不推荐 |
| 16 | 等待选择（候选为旅行规划、商品文案） | 我要用周报撰写那个 | 周报撰写不在候选中，不直接调用 → 按新需求重新推荐 |
| 17 | T1 处理中（Provider A 以 `--delay 90` 启动） | 预算改成 2 万 | 判定为 `supplement T1`，但不提交，提示"旅行规划正在处理上一条消息，请稍候" |
| 18 | T1 处理中（同上） | 今天星期几？ | `router_answer` 立即回复；约 90 秒后 T1 的结果经推送到达并打印，credit 只结算一次 |
| 19 | Provider B 以 `--no-push` 启动 | 写周报（选择周报撰写后） | 订阅确认中不含该 taskId → 自动改为轮询，结果正常到达并计费 |

### 6.8 异步任务：提交、推送与轮询

#### 6.8.1 超时相关共识

1. **第三方 Agent 内部的超时和重试，CIC 不关心。**
2. **CIC 不同步等待第三方的执行结果。** 第三方 Agent 的运行时长无法预知（深度研究类 Agent 超过 1 分钟是常态），同步等待必然遇到超时，超时后重发又必然导致同一会话被重复提交。因此改为异步任务：CIC 只提交任务，第三方立即返回 taskId，完成后由 CIC 通过推送或查询拿到结果。这依赖 MCP 官方的 tasks 扩展（`io.modelcontextprotocol/tasks`），传输仍是 Streamable HTTP 上的 JSON-RPC，**不需要 WebSocket**。
3. **第一版不做超时重试逻辑。**

#### 6.8.2 流程（CIC 侧"任务监听"组件，`cic/task_watcher.py`）

```
① tools/call：params._meta 的 clientCapabilities 中声明 io.modelcontextprotocol/tasks
② 第三方立即返回：
   - CreateTaskResult{taskId, status: working, pollIntervalMs, ttlMs} → 继续 ③
   - 或直接返回 CallToolResult → 跳到 ⑥ 按 completed 处理
③ 立即为该 taskId 打开订阅：subscriptions/listen(notifications.taskIds=[taskId])
④ 收到订阅确认后，主动补查一次 tasks/get（防止任务在订阅生效前就已结束，规范不保证补发）
⑤ 等待 notifications/tasks；以下情况改为按 pollIntervalMs 轮询 tasks/get：
   - 第三方未声明推送能力，或订阅确认中不包含该 taskId；
   - 订阅连接意外断开，且任务尚未结束。
   推送正常时，每 TASK_BACKSTOP_POLL_SECONDS（默认 30 秒）兜底查询一次，防止丢消息
⑥ 任务进入终态后关闭订阅、停止轮询：
   - completed：result 即 CallToolResult，按 §4.3 校验，读取 com.cic/credit_usage 结算（按 taskId 只结算一次）；
   - failed / cancelled / 过期：按 §5.2 判定失败，不计费
```

#### 6.8.3 规则

- **每轮用户输入对应一个任务**。反问仍通过 `result.structuredContent.status = input_required` 表达，下一轮带 `conversation_id` 再提交新任务。**不使用任务自身的 `input_required` 和 `tasks/update`**：它们面向 MRTR 的结构化请求（如 elicitation 表单），如果让一个任务横跨多轮开放式反问，用户中途离开时，任务的 TTL 和计费都难以处理。
- **同一会话不并发**：上一轮任务进入终态前，不提交该会话的下一轮（§4.3 的保证，§6.2 第 4 条）。
- **计费去重**：推送和轮询可能都拿到完成结果，按 taskId 只结算一次。
- **尊重轮询间隔**：按第三方返回的 `pollIntervalMs` 轮询；间隔可能随任务变化，以最新值为准。
- **TTL 是"放弃等待"的边界**：`ttlMs` 从任务创建起计时，由第三方声明（第三方最清楚自己的 Agent 要跑多久），值可能在任务期间变化，`null` 表示不限时。超过 `createdAt + ttlMs` 仍未结束，CIC 按失败处理、不计费、停止订阅和轮询；第三方在 TTL 之后可以删除任务，此时 `tasks/get` 返回"任务不存在"。异步模式下没有同步超时，TTL 就承担了这个角色。契约要求 `ttlMs ≥ 1 小时` 或为 `null`（§4.3）。
- **请求头**：`tasks/get` / `tasks/cancel` 必须带 `Mcp-Name: <taskId>` 请求头（规范要求，方便第三方的网关把请求路由到持有任务状态的实例）。
- **取消**：第三方须按规范实现 `tasks/cancel`；CIC 第一版不提供用户取消入口。
- **实验阶段的简化**：任务监听组件与 Router 运行在同一进程中，taskId 只存内存；每个进行中的任务一条订阅连接。生产环境的做法见 §13。

### 6.9 Router 的实现机制（Step 6 实测版本）

**1. 运行模型：输入串行处理，任务异步执行**

- `cic-chat` 的主循环一次只处理一条输入：先做意图识别（约 2～3 秒），再按意图分支。处理期间不显示输入提示符。
- 提交第三方任务时只等 Provider 接受任务、返回 taskId（约 0.1 秒），**不等任务执行完**。
- 任务结果由后台的任务监听组件接收（推送、补查或轮询），再由一个后台协程交给 Router：更新会话状态，原样打印 Agent 回复和 credit。
- 因此一个任务执行期间，用户可以继续和 Router 聊天，也可以开启其他任务。验收中 90 秒的长任务期间照常闲聊，结果到达后自动打印。
- 同一会话不并发：会话处于「处理中」时，再补充它只会得到「请稍候」，不会提交。
- 输入提示符和后台打印由 prompt_toolkit 协调（`prompt_async` + `patch_stdout`），后台打印不会打乱正在输入的那一行。

**2. 会话状态：每个 `cic-chat` 进程一份，保存在内存，`/new` 清空**

| 组成 | 内容 |
| --- | --- |
| 对话记录 | 用户发言、Router 回复、第三方 Agent 回复（全文，标注来源 `旅行规划 · T1`）、推荐候选列表、失败提示 |
| 第三方会话 | T1、T2…：Agent、Provider、conversation_id（由 Provider 签发）、状态（处理中 / 等待补充 / 已完成 / 失败）、最后一次回复、最后发给 Agent 的消息（重试用）、进行中的 taskId |
| 待选项 | 推荐候选（附带用户原话和图片，选择后原样转发），或失败后的 [1] 重试 / [2] 重新推荐其他 Agent。用户输入其他内容时作废；如果在意图识别期间有新的失败提示到达，新提示会保留到下一轮 |

**3. 两次 LLM 调用的上下文**

| 调用 | 携带的上下文 | 说明 |
| --- | --- | --- |
| 意图识别（每轮必调） | 最近 12 条对话（每条截断到 300 字）；第三方会话列表（编号、Agent、状态、最后一次回复截断到 200 字）；当前待选项；本次输入和图片数量 | 摘要式上下文，只用于判断意图。LLM 只看到 T1 / T2 这类别名，接触不到真实 conversation_id |
| Router 回复（router_answer / clarify / 无候选时） | 最近 20 条对话，**不截断**，Agent 回复为全文；本轮的特殊要求（澄清、提示重新选择等）追加在 system prompt 中 | 因此 Router 能翻译、总结、润色上面 Agent 的完整结果 |

**4. 第三方 Agent 的上下文**

- 不传 Router 的上下文。每次只发送用户本轮的**原话**（和图片）。多轮上下文由 Provider 通过 conversation_id 自己串联（§6.2 第 5 条、§6.5）。

**5. 当前的局限（生产环境需要补充）**

- 会话只保存在内存，进程退出即丢失；没有持久化，也没有多端同步（§13.1、§13.2）。
- 上下文没有按 token 预算控制，也没有摘要。多轮长回复后，Router 回复的 prompt 会明显变大。
- `/new` 只重置当前会话；之前仍在运行的任务照常结算，结束时只打印一条提示。

## 7. 推荐效果评测（`cic-eval`）

- **Agent 集**：3 个真实 Agent + 约 12 个模拟 Agent，按易混淆分组。例如旅行规划 / 酒店预订 / 签证咨询；周报撰写 / 会议纪要 / 邮件写作；商品文案 / 海报设计 / 商品抠图。每个 Agent 都有描述、标签和 3～5 条示例问题。
- **测试集**（`data/eval_queries.yaml`，约 40 条，每条标注期望的 Agent）：

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| 常规 | ~20 | 需求表述清晰 |
| 易混淆 | ~10 | 在相近的 Agent 之间需要细分辨别 |
| 依赖上下文 | ~5 | 带前几轮对话，例如"那帮我把它写成周报" |
| 无合适 Agent | ~5 | 应当被阈值拦截 |

> 测试问题不得与已登记的示例问题原文重复，避免数据泄漏。

- **对比方案**（先跑 V1，作为"没有示例问题时的推荐准确率"基线，再逐步叠加）：

| 方案 | 做法 |
| --- | --- |
| V1 | 只用画像向量，等同于删除全部示例问题（每个点独立 Embedding，只检索 `kind=profile` 与真正删除示例问题的结果一致）。相当于 LiteLLM 的 Embedding 思路 |
| V2 | V1 + 示例问题向量（多向量，取最大分） |
| V3 | V2 + `qwen3-rerank` |
| V4 | V3 + 查询改写（用意图识别产出的 `search_query` 检索） |

- **指标**：Hit@1、Hit@2、MRR；在选定阈值下"无合适 Agent"类的正确拦截率与正常问题的误拦截率；单次推荐延迟。
- **输出**：各方案的指标对比表 + 逐条错例。用结果决定生产是否上 Rerank，以及阈值取多少。

### 7.1 预实验：只用画像 vs 画像 + 示例问题（Step 4 手工数据）

Step 4 入驻完成后（15 个 Agent，`text-embedding-v4`，1024 维，cosine），用同一份索引把每条问题的得分拆成「画像点最高分」和「示例问题点最高分」分别计算：

| 问题 | 期望 Agent | 只用画像：期望 Agent 得分（排名） | 画像 + 示例问题：得分（排名） | 匹配依据 |
| --- | --- | --- | --- | --- |
| 下周想去北海道玩几天，帮我安排一下 | 旅行规划 | 0.49（1） | 0.63（1） | 示例问题 |
| 帮我把今天开会讨论的内容整理一下 | 会议纪要 | 0.60（1） | 0.71（1） | 示例问题 |
| 帮这双鞋做个宣传（附带图片） | 商品文案 | 0.44（1） | 0.67（1） | 示例问题 |
| 帮我写本周周报 | 周报撰写 | 0.71（1） | 0.88（1） | 示例问题 |
| 今天天气怎么样（无合适 Agent） | — | 最高 0.33 | 最高 0.34 | — |

**初步结论**（样本只有 5 条，正式结论以 Step 5 的评测为准）：

1. **推荐不依赖示例问题**：只用画像时，4 条有期望 Agent 的问题全部排第 1。
2. **示例问题主要拉开了与无关问题的分差，让阈值可以设定**：
   - 用户问题是口语化的任务表述，画像是能力说明，「问题对问题」的相似度天然高于「问题对描述」，所以加入示例问题后，正确匹配的得分整体提高 0.11～0.23。
   - 只用画像时，最低的正确匹配（0.44）与无关问题（0.33）只差 0.11；加入示例问题后，正确匹配都在 0.63 以上，差距拉大到 0.29 以上。
   - 没有可用的阈值，就无法拦截「没有合适 Agent」的请求。
3. **示例问题的附带价值**：能区分易混淆的 Agent，例如会议纪要（0.71）与周报撰写（0.57）；还能给出可解释的「匹配依据」。
4. **副作用**：字面相似会带来误匹配。例如「北海道」那条问题里，家常菜谱因示例问题「给一家三口安排一周的晚餐」得 0.44，机票比价得 0.54。这类近似候选要靠阈值、Rerank（V3）或查询改写（V4）压低。

### 7.2 正式评测结果（Step 5，2026-11-22）

**设置：**
- 15 个 Agent（3 个可调用 + 12 个模拟），全部参与排序。
- 40 条测试问题：常规 20、易混淆 10、依赖上下文 5、无合适 Agent 5；其中 3 条附带图片。
- 模型：`text-embedding-v4`（1024 维，cosine）、`qwen3-rerank`。
- 每条问题只做一次 Embedding，三个方案共用。
- 「依赖上下文」类只用最后一句检索，没有做查询改写。
- 完整报告见 `reports/step5-eval-20261122-1831.md`（含阈值扫描、错例和逐条结果）。

| 方案 | Hit@1 | Hit@2 | MRR | 常规 Hit@1 | 易混淆 Hit@1 | 依赖上下文 Hit@1 | 期望 Agent 得分（最低 / 平均） | 无关问题得分（最高 / 平均） | 可用阈值区间（无关问题全部拦截） | 耗时（平均 / P95） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V1 只用画像（删除示例问题） | 82.9% | 100% | 0.914 | 95% | 70% | 60% | 0.36 / 0.51 | 0.32 / 0.28 | 约 0.33～0.36，误拦截 0% | 169 / 302 ms |
| V2 画像 + 示例问题 | 94.3% | 97.1% | 0.967 | 100% | 80% | 100% | 0.38 / 0.64 | 0.40 / 0.33 | 约 0.41～0.50，误拦截 2.9%（1 条依赖上下文问题） | 170 / 356 ms |
| V3 V2 + Rerank | 94.3% | 100% | 0.971 | 100% | 80% | 100% | 0.58 / 0.71 | 0.39 / 0.34 | 约 0.40～0.55，误拦截 0% | 385 / 612 ms |

> - Hit@k / MRR 统计 35 条有期望 Agent 的问题。
> - 「期望 Agent 得分」取该 Agent 自身的得分，不论排名，三个方案基于同一批问题。
> - V3 的分数是 Rerank 分，与 V1 / V2 的向量分不能直接比较。

**结论：**

1. **删除示例问题（V1 vs V2）**
   - Hit@1 从 94.3% 降到 82.9%（−11.4 个百分点），MRR 从 0.967 降到 0.914。
   - Hit@2 仍为 100%：只靠画像时，正确 Agent 也始终在前 2 名，推荐列表（top_k ≥ 2）照样能覆盖。
   - 损失集中在易混淆类（80% → 70%）和依赖上下文类（100% → 60%）；常规类为 95% vs 100%。
2. **示例问题让阈值变得可用**
   - 期望 Agent 的平均得分从 0.51 提高到 0.64，与无关问题平均分的差距从 0.23 拉大到 0.31。
   - V1 能同时满足「无关问题全部拦截、正常问题不误拦截」的阈值区间只有约 0.03（0.33～0.36），实际上无法稳定设定；V2 的区间约 0.10。
3. **Rerank（V3 vs V2）**
   - Hit@1 相同；Hit@2 从 97.1% 到 100%；MRR 从 0.967 到 0.971。准确率提升有限。
   - 主要收益是分数可分性：期望 Agent 最低分从 0.38 提高到 0.58，阈值区间 0.40～0.55 内无关问题全部拦截、正常问题零误拦截。
   - 代价是平均延迟增加约 215 ms（170 → 385 ms），每次推荐多一次 API 调用。
4. **三个方案都没答对的问题**
   - 「把这份周报内容做成几页汇报用的幻灯片」：字面上的「周报」把周报撰写排到了第 1。
   - 「去大阪玩 4 天，住哪个区域方便」：期望是酒店推荐。V2 被机票比价的示例问题「下个月北京飞大阪哪天最便宜」凭字面相似排到第 1；这条问题与旅行规划本身也有合理歧义。
   - 字面相似是示例问题方案的主要副作用。
5. **局限**
   - 测试集只有 40 条（其中无合适 Agent 类仅 5 条）、Agent 只有 15 个，阈值结论的置信度有限，上线后需要用真实日志重新校准。
   - 依赖上下文类在 V2 / V3 中都排在第 1，但「那住的地方呢」在 V2 只有 0.38 分，会被阈值拦下。这需要 V4 的查询改写解决，V4 待 Step 6 后补跑。

### 7.3 V4（查询改写）补跑结果（Step 6，2026-11-22）

V4 = 意图识别 + V3：意图识别结合前几轮对话给出 `search_query`，用它代替原句检索，再走 Rerank。意图不是 `recommend` 的问题视为 Router 不推荐，等同被拦截。完整报告见 `reports/step5-eval-20261122-2126.md`。

| 方案 | Hit@1 | Hit@2 | MRR | 常规 | 易混淆 | 依赖上下文 | 拦截率 | 误拦截率 | 耗时（平均 / P95） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V3 | 94.3% | 100% | 0.971 | 100% | 80% | 100% | 100% | 0% | 371 / 585 ms |
| V4 | 80.0% | 80.0% | 0.800 | 90% | 70% | 60% | 100% | 20.0% | 3028 / 4144 ms |

**结论：**

1. **效果不升反降**。下降几乎全部来自意图层面，而不是改写后的检索：35 条有期望 Agent 的问题中，有 7 条没有被判为 `recommend`，于是没有推荐。
   - 判为 `router_answer`（LLM 认为自己能答）：「去泰国旅游要不要提前办签证」「给合作方写一封邮件，说明项目要延期一周」「帮我整理成给领导看的版本」；
   - 判为 `clarify`（缺少要处理的内容）：「把今天的部门例会记录整理一下…」「把本周三个项目的进展写成给老板的汇报」「把这份周报内容做成几页汇报用的幻灯片」「帮我整理一下要点」。
2. **背后是一个产品问题**：LLM 自己就能完成的任务（常识问答、写邮件、整理文字），要不要仍然推荐专业 Agent？当前规则倾向于由 Router 自己回答，所以评测集中按「应推荐」标注的这类问题被判为「不推荐」。
3. **意图层面的拦截效果很好**：无合适 Agent 类问题中，计算、笑话、编程问题都直接判为 `router_answer`。无关问题的平均最高分从 0.34 降到 0.13。
4. **延迟**：多一次意图识别调用，平均约 3 秒。在 Router 中意图识别本来每轮都要做，所以实际增量只有检索和 Rerank。

**决策（决策 35）**：第一版不使用查询改写，推荐时用用户原话检索。上线后结合真实数据，再决定是否启用改写，以及如何调整「LLM 能答的任务是否也推荐 Agent」的规则。

### 7.4 意图识别评测（Step 6，2026-11-22）

`cic-intent-eval`：`data/intent_cases.yaml` 共 34 条用例（§6.7 的 19 个验收场景加变体），模型为 `qwen4-max`（structured output，`enable_thinking=false`，`temperature=0`）。

| 期望意图 | 用例数 | 准确率 |
| --- | --- | --- |
| supplement | 7 | 100% |
| select_candidate | 6 | 100% |
| router_answer | 10 | 100% |
| recommend | 8 | 100% |
| clarify | 3 | 100% |
| **总体** | **34** | **100%**（延迟 平均 2.3 秒 / P95 3.3 秒） |

局限：用例由设计方编写，规模小，与 prompt 中的规则高度一致，结果偏乐观；上线后需要用真实对话抽样复核。

## 8. 配置（`.env`，不提交）

```dotenv
DASHSCOPE_BASE_URL=
DASHSCOPE_API_KEY=
ROUTER_MODEL=qwen4-max             # 意图识别 + Router 回复
PROVIDER_MODEL=qwen4-max           # 第三方 Agent（图片输入）
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
RERANK_MODEL=qwen3-rerank
RERANK_ENABLED=true                # §7.2 / 决策 30
RECALL_TOP_N=20
RECOMMEND_TOP_K=2
RECOMMEND_MIN_SCORE_DENSE=0.45     # 关闭 Rerank 时使用（决策 31）
RECOMMEND_MIN_SCORE_RERANK=0.48    # 决策 31
QDRANT_PATH=./data/qdrant
OSS_REGION=
OSS_ENDPOINT=
OSS_BUCKET=
OSS_ACCESS_KEY_ID=
OSS_ACCESS_KEY_SECRET=
PROVIDER_A_API_KEY=sk-provider-a-demo
PROVIDER_B_API_KEY=sk-provider-b-demo
PROVIDER_TASK_TTL_MS=3600000       # 模拟 Provider：任务保留时长
PROVIDER_POLL_INTERVAL_MS=2000     # 模拟 Provider：建议的轮询间隔
TASK_BACKSTOP_POLL_SECONDS=30      # CIC：推送正常时的兜底查询间隔
```

Agent 入驻数据（基础信息、示例问题、MCP URL、入口 Tool、单价、key 所对应的环境变量名）写在 `data/agents.yaml` 中，不含明文 key。由于 1 token = 1 Credit，示例单价设为 `0.0001` HKD / Credit。

## 9. 决策记录

| # | 议题 | 结论 |
| --- | --- | --- |
| 1 | 实验项目位置与结构 | `D:\develop\accentrixProject\cic-mcp-lab`，单个 uv 项目（Python 3.12） |
| 2 | `_meta` key 前缀 | 统一使用 `com.cic/`（§5.1） |
| 3 | 调用失败 | 由代码按协议信号判定；失败不计费，不要求返回 `credit_usage`；之后如何回应由 Router 决定（§5.2） |
| 4 | conversation_id | 由 Provider 签发（§6.5） |
| 5 | 反问信号 | `structuredContent.status`：`input_required` / `completed`（§6.5） |
| 6 | Router 编排方式 | 代码编排：意图识别 → 分支（§6） |
| 7 | 推荐方式 | 向量召回 top_k（实验取 2，生产取 5），由用户选择；Rerank 可开关；暂不做 BM25 |
| 8 | 向量库 | 实验用 Qdrant 本地模式，生产用 pgvector，通过 `VectorStore` 接口隔离 |
| 9 | Agent 回复展示 | 直接展示，Router 不改写 |
| 10 | 选定 Agent 后转发的内容 | 用户原话，不携带上下文；对已有内容的纯文本处理（如翻译）由 Router 自己完成 |
| 11 | 示例问题字段 | 3～10 条；实验先跑无示例问题的基线 V1 |
| 12 | 鉴权生效检查未通过 | 直接阻断提交，并在 UI 上报错 |
| 13 | 旧版 MCP Server | 不兼容，只支持 `2026-07-28` |
| 14 | OSS Bucket | 公共读 |
| 15 | 模型 | Router 与第三方 Agent 统一使用 `qwen4-max`；Embedding 用 `text-embedding-v4`；Rerank 用 `qwen3-rerank` |
| 16 | Mock credit 规则 | 1 token = 1 Credit，`credit_usage = total_tokens` |
| 17 | Agent 选择 | 第一版不做快捷选择，每轮输入都经过意图识别；只能从 Router 推荐的候选中选择 |
| 18 | 超时与重试 | 第三方内部的超时和重试 CIC 不关心；CIC 不同步等待第三方结果；第一版不做超时重试（§6.8.1） |
| 19 | 调用方式 | 异步任务，采用 MCP tasks 扩展；第三方可直接返回结果，也可返回任务，CIC 两种都处理；模拟 Provider 统一走任务（§6.8） |
| 20 | 结果获取 | 第一版即实现推送（`subscriptions/listen` + `notifications/tasks`），轮询兜底；订阅后补查一次（§6.8.2） |
| 21 | 同一会话并发 | CIC 保证同一 `conversation_id` 最多一个未结束的调用或任务，Provider 无需处理并发（§4.3） |
| 22 | 任务计费 | 只有 `completed` 的任务计费，按 taskId 只结算一次；failed / cancelled / 过期不计费（§5） |
| 23 | 反问与任务 | 每轮用户输入对应一个任务；反问仍用 `structuredContent.status`，不使用任务自身的 `input_required`（§6.8.3） |
| 24 | 多租户规模 | 注意事项写入 §13，实验阶段不实现 |
| 25 | 任务 TTL | `ttlMs` 是"放弃等待"的边界，由第三方声明；契约要求 ≥ 1 小时或为 `null`，不达标只记录违规、不拦截（§4.3、§6.8.3） |
| 26 | Agent 注册表 | 实验用 `data/agents.yaml` 作种子数据；生产环境注册表入库并管理上下架状态（§13.10）。上下架的演示放到 Step 4 |
| 27 | 实验中的下架方式 | 手工修改 `data/agents.yaml` 的 `status`（published / delisted），模拟运营后台操作；推荐展示前和提交任务时都实时读注册表，下架立即生效；`cic-onboard` 负责同步向量索引（下架只改状态，不需要 Provider 在线） |
| 28 | Rerank 的实现时机 | 移到 Step 5，与评测 V3 一起实现；Step 4 只做向量召回，阈值默认不启用 |
| 29 | Step 5 评测范围 | Step 5 做 V1（只用画像，等同于删除示例问题）/ V2（画像 + 示例问题）/ V3（+ Rerank）；V4（查询改写）依赖 Step 6 的意图识别，届时用同一套评测补跑 |
| 30 | Rerank 默认开关 | 默认启用（`RERANK_ENABLED=true`）。依据 §7.2：准确率与 V2 持平，但只有 V3 能同时做到无关问题全部拦截、正常问题零误拦截；代价是推荐那一轮平均多约 215 ms |
| 31 | 推荐阈值 | `RECOMMEND_MIN_SCORE_RERANK=0.48`（评测可用区间 0.40～0.55，最佳点 0.488）；关闭 Rerank 时 `RECOMMEND_MIN_SCORE_DENSE=0.45`（区间 0.41～0.50，最佳点 0.466）。样本量小，上线后用真实日志重新校准 |
| 32 | Router 侧的 LLM 调用 | 不用 agentscope，直接用 OpenAI SDK 调百炼兼容接口：意图识别用 `chat.completions.parse`（strict json_schema，`enable_thinking=false`，实测约 2 秒）；Router 回复用 `chat.completions.create`。意图识别和回复分两次调用 |
| 33 | `cic-chat` 交互 | prompt_toolkit 异步输入 + `patch_stdout`，任务结果在用户输入时也能打印，且不打乱输入行 |
| 34 | 调用失败后的交互 | Router 提示「暂时不可用（不计费）」，并给出待选项 [1] 重试（同一 conversation_id 重发失败的消息）/ [2] 重新推荐其他 Agent（用失败的消息推荐，排除失败的 Agent）；用户输入其他内容时作废 |
| 35 | 查询改写 | 第一版不使用：V4 的 Hit@1 为 80%，低于 V3 的 94.3%（§7.3）；推荐时用用户原话检索，`search_query` 只用于日志。上线后看真实数据再决定 |

## 10. 项目结构

```
cic-mcp-lab/
├─ pyproject.toml
├─ .env.example
├─ docs/design.md                    # 本文档（不提交）
├─ data/
│  ├─ agents.yaml                    # Agent 入驻数据：3 个真实 + ~12 个模拟
│  ├─ eval_queries.yaml              # 推荐评测集
│  └─ qdrant/                        # 本地向量库（gitignore）
├─ src/cic_mcp_lab/
│  ├─ config.py                      # 读取 .env
│  ├─ llm.py                         # 百炼 chat / embedding / rerank 客户端工厂
│  ├─ provider/
│  │  ├─ catalog.py                  # Provider A/B 名下的 Agent 定义（人设、Tool 名、描述）
│  │  ├─ agent.py                    # 第三方 Agent（反问 + 多模态 + 按会话记忆）
│  │  ├─ credit.py                   # token → credit 换算
│  │  ├─ cli.py                      # 直接与 Agent 对话（provider-agent，不经过 MCP）
│  │  ├─ tasks.py                    # tasks 扩展服务端：任务存储、tasks/get、tasks/cancel、taskIds 订阅推送
│  │  └─ server.py                   # MCP Server（provider-server --provider A --port 8001 [--delay N] [--no-push]）
│  └─ cic/
│     ├─ registry.py                 # 加载 agents.yaml
│     ├─ oss.py                      # OSS 上传
│     ├─ mcp_client.py               # MCP Client 封装 + 契约 / credit_usage 校验 + 两种返回形态
│     ├─ task_watcher.py             # 任务监听：订阅推送、补查、轮询兜底、按 taskId 结算一次
│     ├─ probe.py                    # 测试连接（cic-probe <agent_id>）
│     ├─ onboard.py                  # 入驻：测试连接 → 画像 → Embedding → 写入（cic-onboard）
│     ├─ recommend/
│     │  ├─ embedding.py             # Embedding 工具
│     │  ├─ vector_store.py          # VectorStore 接口 + Qdrant 本地实现
│     │  ├─ rerank.py                # Rerank（可开关，Step 5 实现）
│     │  ├─ recommender.py           # 召回 → 聚合 → 回查注册表 → (Rerank) → 阈值 → top_k
│     │  └─ cli.py                   # 命令行查询推荐结果（cic-recommend）
│     ├─ intent.py                   # 意图识别工具
│     ├─ session.py                  # 会话状态（threads、pending）
│     ├─ ledger.py                   # Credit 账本
│     ├─ router.py                   # 代码编排
│     ├─ cli.py                      # 交互 CLI（cic-chat）
│     └─ eval.py                     # 推荐评测（cic-eval）
└─ tests/
```

## 11. 分步实施（每步独立验证，确认后再进入下一步）

| 步骤 | 内容 | 验证方式 |
| --- | --- | --- |
| 0 ✅ | uv 初始化、依赖、`.env`；百炼 chat、embedding 与 OSS 的连通性冒烟 | qwen 回复一句话；embedding 返回 1024 维；上传图片后能在浏览器打开 URL |
| 1 ✅ | 第三方 Agent（本地直接调用，3 个） | 信息不足时返回 `input_required`，补全后返回 `completed`；能识别图片 URL；打印 token 与 credit |
| 2 ✅ | MCP Server + tasks 扩展服务端，启动 A、B 两个实例 | 无 key 返回 401；A 的 key 访问不了 B；`tools/call` 在 `--delay 90` 下也能立即（< 1 秒）返回 taskId；`tasks/get` 与 `notifications/tasks` 拿到同样的完成结果，`result._meta` 含 credit_usage；`--no-push` 时订阅确认不含 taskId；`tasks/cancel` 生效；任务按 TTL 过期 |
| 3 ✅ | MCP Client + 任务监听 + `cic-probe` | 3 个 Agent 测试连接全部通过；构造一个不合规的 Tool，能被契约校验拦截；推送、补查、轮询兜底三条路径都能拿到结果；同一任务只结算一次；§5.2 的各类失败都判定为失败且不计费；直接返回结果的 Server 也能正常处理；任务进行中连接断开时重连并改为轮询，连续 3 次失败判定为丢失 |
| 4 ✅ | 入驻向量化 + 推荐器（`cic-onboard`、`cic-recommend`） | 15 个 Agent 写入 Qdrant；用命令行查询能返回带分数和匹配依据的 top_k；下架一个 Agent 后它不再被推荐、提交被拒，在途任务照常结算（§13.10） |
| 5 ✅ | 推荐评测（`cic-eval`） | 输出 V1～V3 指标对比（V4 待 Step 6 后补跑）；确定阈值和 Rerank 开关。结果见 §7.2 |
| 6 ✅ | 意图识别 + Router 编排 + `cic-chat` | §6.7 的 19 个验收场景通过（场景 14 按 14a / 14b 验证）；意图识别准确率见 §7.4；V4 结论见 §7.3；Router 的实现机制见 §6.9 |
| 7 | 结论回填 | 输出入驻字段与校验规则、入口 Tool 契约、credit_usage 协议草案、推荐方案，供 Portal 页面和 API 文档改造 |

## 12. 风险与待验证点

- AgentScope 2.0.12 如何拿到单次 reply 的 token usage（模型响应中的 usage，或 hook）。
- `mcp` 2.3.0 服务端如何挂载鉴权中间件（自定义 ASGI 中间件，或 SDK 自带的 TokenVerifier）。
- `mcp` 2.3.0 的 `MCPServer` 是否默认把 tool 函数抛出的异常包装成 `isError: true` 的结果（§5.2 依赖这一行为）。
- `qwen4-max` 官方支持图像输入、工具调用和结构化输出；需验证三者在百炼兼容接口的同一请求中能否同时使用。
- 百炼服务端能否拉取 OSS 公网图片 URL（地域、防盗链 Referer 白名单）。
- `qwen3-rerank` 在 OpenAI 兼容模式下的具体接口路径。
- text-embedding-v4 的 OpenAI 兼容接口不区分 query / document 两种文本类型（DashScope 原生接口支持），可能略微影响检索效果；由评测验证。
- Qdrant 本地模式同一时间只允许一个进程打开：`cic-onboard` 与 `cic-chat` / `cic-eval` 不能同时运行。
- `mcp` 2.3.0 没有现成的 tasks 实现，需要用扩展框架自己实现：服务端注册 `tasks/get`、`tasks/cancel`，拦截 `tools/call` 返回 `CreateTaskResult`；客户端识别 `resultType: "task"`、接收 `notifications/tasks`。需验证：`subscriptions/listen` 的过滤条件能否携带扩展字段 `taskIds`，以及 SDK 是否允许扩展发送自定义通知。写第 2 步计划前先做小验证。
- 订阅连接在 Windows 本地、长时间（≥ 90 秒）空闲时是否稳定；SDK 是否有心跳或超时设置。（已验证：服务端每 15 秒发 SSE ping，客户端读超时须大于 15 秒；实测 90 秒级推送正常）
- 本机 httpx 会读取 Windows 系统代理（如 `127.0.0.1:7890`），连 `127.0.0.1` 也走代理。第 3 步 CIC 的 MCP Client 连本地 Provider 时，应使用 `trust_env=False` 的 httpx 客户端（SDK 的 `create_mcp_http_client` 默认会读环境代理）。

## 13. 生产环境注意事项（多租户规模，实验阶段不实现）

Router 和任务监听要服务大量企业及其员工，而不是单个用户或几十人。以下事项在实验中不实现，落地时需要考虑。

### 13.1 等待与用户请求解耦

- 任务监听做成**独立组件**（独立服务或后台 worker），持有订阅和轮询；用户的 HTTP 请求只负责"提交任务"，不挂起等待结果。
- 结果到达后，通过 CIC 自己的通道（SSE 或 WebSocket）推送给 org-portal 前端；前端断线重连后，可以按会话拉取最新状态。
- 这样第三方 Agent 运行多久都不会占用 CIC 的请求处理能力。

### 13.2 任务持久化与故障恢复

- 规范建议客户端持久化 taskId。生产环境需记录：taskId、Provider、Agent、conversation_id、org / project / user、`com.cic/request_id`、提交时间、状态、`ttlMs`、最近一次 `pollIntervalMs`。
- 任务监听实例重启后，从存储中恢复所有未结束的任务，重新订阅或轮询，保证结果不丢。

### 13.3 水平扩展与连接数

- **推送的连接数随在跑任务数线性增长**：订阅的过滤条件在打开时固定，基础做法是每个任务一条连接。并发任务达到上千时，需要考虑：
  - 按 Provider 合并订阅：定期用"当前在跑的 taskId 集合"重建一条订阅连接，重建期间用 `tasks/get` 补查；
  - 限制每个 Provider 的订阅连接数和查询 QPS。
- **轮询的请求量** ≈ 在跑任务数 ÷ 轮询间隔。都是异步 I/O，Python asyncio 进程的瓶颈通常是连接数和内存，而不是 CPU；可通过增加实例水平扩展。
- **多实例分工**：按 taskId 或 Provider 把任务分配给不同的监听实例，并用租约（lease）机制避免两个实例重复处理同一任务；实例宕机后，租约到期，由其他实例接管。

### 13.4 网关与负载均衡

- 订阅是 SSE 长连接，网关和负载均衡的空闲超时会切断它，需要调整超时或依赖心跳；断开后按 §6.8.2 重新订阅并补查。
- `tasks/get` / `tasks/cancel` 必须带 `Mcp-Name: <taskId>` 请求头（规范要求），方便第三方把请求路由到持有任务状态的实例。CIC 自身的网关也不能剥离这个请求头。

### 13.5 公平性、限流与配额

- 按租户（org / project）和 Provider 设置并发任务上限与配额，防止单个租户或单个慢 Provider 拖垮整体。
- 遵守第三方给出的 `pollIntervalMs`，同时设置下限，防止过于频繁的轮询。

### 13.6 幂等与计费一致性

- 提交 `tools/call` 时携带 `com.cic/request_id`，第三方据此去重，解决"任务已提交成功，但响应丢失后重发"导致的重复任务（第一版不做重试，暂不需要）。
- 结算按 taskId 幂等，且与任务状态更新放在同一事务中；计量上报携带 `request_id`，便于对账。

### 13.7 任务生命周期

- 遵守 `ttlMs`：超过 `createdAt + ttlMs` 仍未结束，视为失败，不计费。
- **TTL 带来的计费风险**：任务已完成，但 CIC 在 TTL 内没取走结果（如任务监听组件宕机），结果会丢失，CIC 不计费，而第三方的成本已经产生。需要靠 taskId 持久化与重启恢复（§13.2）、契约中的 TTL 最小值（§4.3）共同降低这一风险。
- 用户离开后仍在运行的任务：继续接收结果并写入会话，下次打开时可见；或在用户删除会话时调用 `tasks/cancel`。

### 13.8 隔离与安全

- 各 Provider 的 API Key 加密存储（如 KMS），只在调用时解密。
- 对第三方返回的内容做大小限制和安全过滤（结果会直接展示给用户）。
- 图片 URL 只传 `https://` 且来自 CIC 自己的 OSS。

### 13.9 可观测性

- `traceparent` 贯穿"提交 → 推送 / 轮询 → 结算"全链路。
- 关键指标：在跑任务数、提交耗时、任务完成耗时分布、推送覆盖率（经推送拿到结果的比例）、按 Provider 统计的失败率和超时率。

### 13.10 Agent 注册表与上下架

实验中的 `data/agents.yaml` 只是种子数据。生产环境随时会有 Agent 上新或下架，注册表必须是动态的。

- **数据来源**：注册表存数据库。Provider Portal 提交，Operation Portal 审核。
  - 状态：草稿 → 测试连接通过 → 审核中 → 已上架（published）→ 已下架（delisted）/ 暂停（suspended）。
  - 编辑会生成新版本；新版本审核通过前，旧版本继续服务。
- **读取时按状态过滤**：
  - 推荐：向量检索带 `status=published` 过滤（§4.5）。因为索引更新可能有延迟，Router 展示候选前再向注册表确认一次。
  - 提交：每次提交都实时查注册表，非 `published` 直接拒绝，提示「Agent 已下架」，不计费。
  - 在途任务：使用提交时刻的快照（mcp_url、key、入口 Tool、单价）继续跟踪和结算，下架不影响已提交的任务。
  - 进行中的多轮会话：下一轮时如果 Agent 已下架，Router 告知用户，并重新推荐。
- **变更传播**：上下架和编辑通过事件（outbox → MQ / Redis pub/sub）通知 Router 与任务监听刷新本地缓存（缓存设短 TTL 兜底），同一事件的消费者负责更新向量索引。
- **第三方私自改动**：Provider 可能改了 Tool 结构、吊销了 key 或关停了服务。所以要定期自动执行测试连接（§4.2），对比审核时的 fingerprint 和入口 Tool 契约；不符合时自动暂停该 Agent，并通知 Provider。
- **实验代码的对应关系**：TaskWatcher 每次提交时按 `agent_id` 查询注册表，并把查到的记录作为快照传给该任务的跟踪协程。换成数据库时只需替换注册表的实现。
