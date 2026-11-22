"""API-key authentication for the provider MCP servers (SDK TokenVerifier)."""
from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken


class ApiKeyVerifier:
    """Accepts `Authorization: Bearer <key>` for a static key → caller-id table."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    async def verify_token(self, token: str) -> AccessToken | None:
        for key, client_id in self._keys.items():
            if hmac.compare_digest(key.encode(), token.encode()):
                return AccessToken(token=token, client_id=client_id, scopes=[])
        return None
