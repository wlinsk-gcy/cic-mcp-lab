# cic-mcp-lab

CIC × MCP 第三方 Agent 接入实验（CLI）。

## 准备

```powershell
uv sync
Copy-Item .env.example .env   # 然后填写百炼与 OSS 配置
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `uv run cic-smoke` | 百炼 chat / embedding 与 OSS 上传的连通性检查 |
| `uv run provider-agent --agent <trip_planner\|product_copy\|weekly_report>` | 直接与模拟第三方 Agent 对话（不经过 MCP）；REPL 内可用 `/image <本地路径或URL>`、`/new`、`/exit` |
| `uv run provider-server --provider <A\|B> --port <端口> [--delay 秒] [--no-push] [--no-tasks]` | 启动模拟 Provider 的 MCP Server（API Key 鉴权，tasks 扩展；`--no-tasks` 时直接返回结果） |
| `uv run provider-check --port <端口> --key-env <变量名> --agent <id> --message <文本>` | 在协议层面检查一个 Provider MCP Server：鉴权、任务、推送或轮询、credit_usage |
| `uv run cic-probe <agent_id> \| --all` | 对 `data/agents.yaml` 中已入驻的 Agent 执行测试连接：discover → 鉴权生效检查 → tools/list → 入口 Tool 契约校验 |
| `uv run cic-call --agent <id> --message <文本> [--conversation-id <id>] [--image <路径或URL>]` | 经 MCP 调用已入驻的 Agent：提交任务 → 推送 / 补查 / 轮询拿结果 → 校验契约并按 credit_usage 结算 |
| `uv run cic-onboard <agent_id>... \| --all` | 入驻：测试连接（可调用 Agent）→ 画像 + 示例问题 Embedding → 写入本地 Qdrant；`status: delisted` 的 Agent 只同步状态。与其他使用 Qdrant 的命令不能同时运行 |
| `uv run cic-recommend "<问题>" [--image] [--include-mock] [--top-k N] [--rerank]` | 按问题推荐 Agent：向量召回 → 按 Agent 聚合 → 回查注册表（下架立即生效）→ top_k，显示匹配度与依据 |
| `uv run cic-eval [--variants V1,V2,V3] [--output <路径>]` | 推荐效果评测：V1 只用画像（等同于删除示例问题）/ V2 画像 + 示例问题 / V3 + Rerank；输出 Hit@1/2、MRR、阈值扫描、错例，报告写入 `reports/` |
| `uv run pytest -q` | 运行单元测试 |

## Agent 上下架（实验）

`data/agents.yaml` 中每个 Agent 的 `status` 为 `published` 或 `delisted`，修改它即模拟运营后台的上下架操作：

- 推荐展示前、提交任务时都会实时读取注册表，所以下架立即生效；已提交的任务照常结算。
- 运行 `uv run cic-onboard <agent_id>` 同步向量索引：下架只改状态；重新上架会重新测试连接并写入向量。
