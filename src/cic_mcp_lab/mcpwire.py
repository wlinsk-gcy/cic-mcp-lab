"""Minimal raw JSON-RPC client for MCP 2026-07-28 over Streamable HTTP (wire-level checks)."""
from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


@dataclass(frozen=True)
class WireResponse:
    status_code: int
    headers: httpx.Headers
    body: dict[str, Any] | None

    @property
    def result(self) -> dict[str, Any]:
        if not self.body or "result" not in self.body:
            raise RuntimeError(f"No JSON-RPC result (HTTP {self.status_code}): {self.body}")
        return self.body["result"]

    @property
    def error(self) -> dict[str, Any] | None:
        return (self.body or {}).get("error")


class WireClient:
    def __init__(self, url: str, api_key: str | None) -> None:
        self._url = url
        self._api_key = api_key
        # The server pings listen streams every 15 s, so the read timeout must be well above that.
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, read=300.0))
        self._ids = itertools.count(1)

    def __enter__(self) -> WireClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tasks: bool = True,
        name: str | None = None,
    ) -> WireResponse:
        resp = self._http.post(
            self._url,
            json=self._payload(method, params or {}, tasks),
            headers=self._headers(method, name),
        )
        return WireResponse(resp.status_code, resp.headers, _parse_body(resp))

    def call_tool(self, tool: str, arguments: dict[str, Any], *, tasks: bool = True) -> WireResponse:
        return self.request("tools/call", {"name": tool, "arguments": arguments}, tasks=tasks, name=tool)

    def get_task(self, task_id: str) -> WireResponse:
        return self.request("tasks/get", {"taskId": task_id}, name=task_id)

    def cancel_task(self, task_id: str) -> WireResponse:
        return self.request("tasks/cancel", {"taskId": task_id}, name=task_id)

    @contextmanager
    def listen_tasks(self, task_ids: list[str]) -> Iterator[Iterator[dict[str, Any]]]:
        """Open a `subscriptions/listen` stream for task ids; yields an iterator of JSON-RPC messages."""
        method = "subscriptions/listen"
        payload = self._payload(method, {"notifications": {"taskIds": task_ids}}, True)
        with self._http.stream("POST", self._url, json=payload, headers=self._headers(method, None)) as resp:
            resp.raise_for_status()
            yield (json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:"))

    def _headers(self, method: str, name: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if name is not None:
            headers["Mcp-Name"] = name
        return headers

    def _payload(self, method: str, params: dict[str, Any], tasks: bool) -> dict[str, Any]:
        capabilities = {"extensions": {TASKS_EXTENSION: {}}} if tasks else {}
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": capabilities,
            "io.modelcontextprotocol/clientInfo": {"name": "cic-mcp-lab-wire", "version": "0"},
        }
        return {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": {**params, "_meta": meta}}


def _parse_body(resp: httpx.Response) -> dict[str, Any] | None:
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        frames = [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
        responses = [frame for frame in frames if "id" in frame and ("result" in frame or "error" in frame)]
        return responses[-1] if responses else None
    if content_type.startswith("application/json"):
        return resp.json()
    return None
