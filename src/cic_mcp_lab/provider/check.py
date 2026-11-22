"""Wire-level manual check of one provider MCP server (step 2 checkpoint)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from ..config import load_settings
from ..mcpwire import TASKS_EXTENSION, WireClient
from .tasks import TERMINAL_STATUSES


def _wait_for_task(client: WireClient, task_id: str) -> dict[str, Any]:
    with client.listen_tasks([task_id]) as messages:
        accepted = next(messages)["params"]["notifications"].get("taskIds", [])
        print(f"[listen] 订阅确认 taskIds={accepted}")
        if task_id in accepted:
            for message in messages:
                params = message.get("params", {})
                if message.get("method") == "notifications/tasks" and params.get("status") in TERMINAL_STATUSES:
                    print("[push] 收到任务结束推送")
                    return params
    print("[poll] 未通过推送拿到结果，改为轮询 tasks/get")
    while True:
        task = client.get_task(task_id).result
        if task["status"] in TERMINAL_STATUSES:
            return task
        time.sleep(task["pollIntervalMs"] / 1000)


def _run(args: argparse.Namespace, api_key: str) -> bool:
    url = f"http://127.0.0.1:{args.port}/mcp"
    with WireClient(url, None) as anonymous:
        status = anonymous.request("server/discover").status_code
    print(f"[auth] 不带 key：HTTP {status}")
    if status != 401:
        return False

    with WireClient(url, api_key) as client:
        discover = client.request("server/discover")
        if discover.status_code != 200:
            print(f"[auth] key 被拒绝：HTTP {discover.status_code}")
            return False
        extensions = discover.result["capabilities"].get("extensions", {})
        print(f"[discover] 声明 tasks 扩展：{TASKS_EXTENSION in extensions}")
        print(f"[tools] {[tool['name'] for tool in client.request('tools/list').result['tools']]}")

        arguments: dict[str, Any] = {"message": args.message}
        if args.conversation_id:
            arguments["conversation_id"] = args.conversation_id
        if args.image:
            arguments["image_urls"] = args.image
        started = time.monotonic()
        created = client.call_tool(args.agent, arguments)
        if created.error:
            print(f"[call] JSON-RPC error：{created.error}")
            return False
        result = created.result
        print(f"[call] {time.monotonic() - started:.2f}s 返回 resultType={result['resultType']}")
        if result["resultType"] == "task":
            print(f"[task] taskId={result['taskId']} pollIntervalMs={result['pollIntervalMs']} ttlMs={result['ttlMs']}")
            task = _wait_for_task(client, result["taskId"])
            print(f"[task] status={task['status']}，从提交起共 {time.monotonic() - started:.1f}s")
            if task["status"] != "completed":
                print(json.dumps(task, ensure_ascii=False, indent=2))
                return False
            again = client.get_task(result["taskId"]).result
            print(f"[task] tasks/get 与推送/轮询结果一致：{again.get('result') == task['result']}")
            tool_result = task["result"]
        else:
            tool_result = result

    print(json.dumps(tool_result.get("structuredContent"), ensure_ascii=False, indent=2))
    print(f"[credit] com.cic/credit_usage={tool_result.get('_meta', {}).get('com.cic/credit_usage')}")
    if tool_result.get("isError", False):
        content = tool_result.get("content") or [{}]
        print(f"[error] {content[0].get('text', '(no error text)')}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="在协议层面检查一个 Provider MCP Server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--key-env", required=True, help="保存 API Key 的环境变量名，如 PROVIDER_A_API_KEY")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--conversation-id")
    parser.add_argument("--image", action="append", help="https 图片地址，可重复")
    args = parser.parse_args()
    load_settings()  # loads .env into the process environment
    api_key = os.environ.get(args.key_env, "").strip()
    if not api_key:
        raise SystemExit(f"Missing {args.key_env} in .env")
    sys.exit(0 if _run(args, api_key) else 1)
