"""A tiny MCP server on stdio, for the gateway tests. FAKE_DESCRIPTION changes the description of send_email."""
import json
import os
import sys

TOOLS = [
    {"name": "send_email", "description": os.environ.get("FAKE_DESCRIPTION", "Send an email."),
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "delete_email", "description": "Delete an email.", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "pay", "description": "Pay an invoice.", "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}}}},
]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg.get("method")
    if m == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        result = {"tools": TOOLS}
    elif m == "tools/call":
        p = msg["params"]
        with open(os.environ["FAKE_LOG"], "a") as fh:
            fh.write(json.dumps(p) + "\n")
        result = {"content": [{"type": "text", "text": f"done {p['name']} {json.dumps(p.get('arguments'), sort_keys=True)}"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
