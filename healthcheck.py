"""Docker healthcheck: calls list_device_names via the MCP Streamable HTTP endpoint."""

import sys

import httpx

resp = httpx.post(
    "http://localhost:8080/mcp",
    headers={"Accept": "application/json", "Content-Type": "application/json"},
    json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_device_names", "arguments": {}},
    },
    timeout=5,
)
resp.raise_for_status()
body = resp.json()
if "error" in body:
    print(f"MCP error: {body['error']}", file=sys.stderr)
    sys.exit(1)
