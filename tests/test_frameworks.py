"""The guarded function plugs into the usual agent frameworks unchanged: they read its name, signature and
docstring, and the model gets Tool Guard's message as the tool's answer when the call is blocked."""

import asyncio
import json
import unittest

from tool_guard import APPROVAL, Guard, limit

guard = Guard()


@guard.tool()
@limit("amount", max=50, above=APPROVAL, min=0)
def refund(order: str, amount: float) -> str:
    """Refund an order. The amount is in euros."""
    return f"refunded {amount}"


class FrameworksTest(unittest.TestCase):
    def test_openai_agents_sdk(self):
        try:
            from agents import function_tool
            from agents.tool_context import ToolContext
        except ImportError:
            self.skipTest("openai-agents not installed")
        t = function_tool(refund)
        self.assertEqual("refund", t.name)
        self.assertIn("Refund an order", t.description)
        self.assertEqual({"order", "amount"}, set(t.params_json_schema["properties"]))
        ctx = ToolContext(context=None, tool_name="refund", tool_call_id="1", tool_arguments="{}")
        out = asyncio.run(t.on_invoke_tool(ctx, json.dumps({"order": "A", "amount": 180})))
        self.assertIn("NOT done yet", str(out))
        out = asyncio.run(t.on_invoke_tool(ctx, json.dumps({"order": "A", "amount": 20})))
        self.assertEqual("refunded 20.0", str(out))

    def test_langchain(self):
        try:
            from langchain_core.tools import tool
        except ImportError:
            self.skipTest("langchain-core not installed")
        t = tool(refund)
        self.assertEqual("refund", t.name)
        self.assertEqual({"order", "amount"}, set(t.args))
        self.assertIn("NOT done yet", t.invoke({"order": "A", "amount": 180}))
        self.assertEqual("refunded 20.0", t.invoke({"order": "A", "amount": 20}))

    def test_mcp_server(self):
        try:
            from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
        except ImportError:
            try:
                from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
            except ImportError:
                self.skipTest("mcp not installed")
        server = Server("shop")
        server.tool()(refund)
        tools = asyncio.run(server.list_tools())
        self.assertEqual(["refund"], [t.name for t in tools])
        self.assertEqual({"order", "amount"}, set((getattr(tools[0], "input_schema", None) or tools[0].inputSchema)["properties"]))
        out = asyncio.run(server.call_tool("refund", {"order": "A", "amount": 180}))
        self.assertIn("NOT done yet", json.dumps(out, default=str))


if __name__ == "__main__":
    unittest.main()
