"""Check the authenticated MCP tool catalog without invoking a model."""

import argparse
import os
import secrets

from cic_mcp_lab.mcpwire import WireClient

_EXPECTED_TOOLS = {"A": {"trip_planner", "product_copy"}, "B": {"weekly_report"}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=_EXPECTED_TOOLS)
    parser.add_argument("url")
    parser.add_argument("--verify-auth", action="store_true", help="also verify that missing and invalid keys return 401")
    args = parser.parse_args()

    key_env = f"PROVIDER_{args.provider}_API_KEY"
    key = os.environ.get(key_env, "").strip()
    if not key:
        raise SystemExit(f"Missing {key_env}")
    provider_id = f"provider-{args.provider.lower()}"
    if args.verify_auth:
        for label, invalid_key in (("missing", None), ("invalid", secrets.token_hex(16))):
            with WireClient(args.url, invalid_key) as client:
                status = client.request("tools/list").status_code
            if status != 401:
                raise SystemExit(f"{provider_id}: {label} key returned HTTP {status}, expected 401")

    with WireClient(args.url, key) as client:
        response = client.request("tools/list")
    if response.status_code != 200:
        raise SystemExit(f"{provider_id}: tools/list returned HTTP {response.status_code}, expected 200")
    names = {tool["name"] for tool in response.result["tools"]}
    expected = _EXPECTED_TOOLS[args.provider]
    if names != expected:
        raise SystemExit(f"{provider_id}: expected {sorted(expected)}, received {sorted(names)}")
    auth_status = " (missing and invalid keys returned 401)" if args.verify_auth else ""
    print(f"{provider_id}: {', '.join(sorted(names))}{auth_status}")


if __name__ == "__main__":
    main()
