"""Docker healthcheck: drives the MCP Streamable HTTP endpoint end-to-end.

Performs the full MCP handshake (initialize -> notifications/initialized ->
tools/call) and asserts that ``list_device_names`` returns a real result. This
exercises the whole path (MCP server -> ESPHome dashboard WebSocket), so the
container only reports healthy when the dashboard is actually reachable and the
tool works — unlike a bare TCP/HTTP probe.
"""

import json
import sys

import httpx

URL = "http://localhost:8080/mcp"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _parse(resp: httpx.Response) -> dict | None:
    """Parse a JSON or SSE (text/event-stream) MCP response into a dict."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


def main() -> None:
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        # 1. initialize -> obtain session id
        init = client.post(
            URL,
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "healthcheck", "version": "1"},
                },
            },
        )
        init.raise_for_status()
        body = _parse(init)
        if not body or "result" not in body:
            print(f"initialize failed: {body}", file=sys.stderr)
            sys.exit(1)

        headers = dict(HEADERS)
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id

        # 2. notifications/initialized (no response expected)
        client.post(
            URL,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        # 3. tools/call list_device_names
        resp = client.post(
            URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_device_names", "arguments": {}},
            },
        )
        resp.raise_for_status()
        body = _parse(resp)
        if not body or "error" in body:
            print(f"tools/call error: {body}", file=sys.stderr)
            sys.exit(1)

        result = body.get("result", {})
        if result.get("isError"):
            print(f"tool returned error: {result}", file=sys.stderr)
            sys.exit(1)

        # Tool errors are returned as plain text (not JSON-RPC errors); treat a
        # dashboard-connectivity failure as unhealthy.
        text = " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        )
        if "Error fetching devices" in text:
            print(f"dashboard unreachable: {text}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
