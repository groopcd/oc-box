"""Tiny offline MCP/LSP/formatter fixtures for the real OpenCode smoke test."""

import json
from pathlib import Path
import sys

kind, log_path = sys.argv[1:3]


def log(value):
    with open(log_path, "a") as stream:
        stream.write(json.dumps(value) + "\n")


def read_message():
    if kind == "mcp":
        line = sys.stdin.buffer.readline()
        return json.loads(line) if line else None
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))


def send(value):
    payload = json.dumps(value).encode()
    if kind == "lsp":
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    else:
        sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


if kind == "format":
    file = Path(sys.argv[3])
    log({"formatted": str(file)})
    file.write_text(file.read_text().upper())
else:
    while True:
        message = read_message()
        if message is None:
            break
        log(message)
        method = message.get("method")
        result = {}
        if method == "initialize":
            if kind == "mcp":
                result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "oc-box-fixture", "version": "1.0"}}
            else:
                result = {"capabilities": {"textDocumentSync": 1}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping", "description": "Return an offline smoke-test marker", "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "MCP_SMOKE_OK"}]}
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": message["params"]["textDocument"]["uri"], "diagnostics": []}})
        if "id" in message:
            send({"jsonrpc": "2.0", "id": message["id"], "result": result})
