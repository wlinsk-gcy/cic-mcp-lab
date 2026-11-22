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
| `uv run pytest -q` | 运行单元测试 |
