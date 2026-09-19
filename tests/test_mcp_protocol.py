"""MCP 协议层测试：真的把 mcp_server.py 拉起来说话（stdio JSON-RPC）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp_server.py")


class McpProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env.setdefault("OCADS_ROOTS", tempfile.gettempdir())
        cls.proc = subprocess.Popen(
            [sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        cls.id = 0

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()          # type: ignore[union-attr]
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def rpc(self, method: str, params: dict | None = None) -> dict:
        McpProtocolTest.id += 1
        payload = {"jsonrpc": "2.0", "id": McpProtocolTest.id, "method": method,
                   "params": params or {}}
        self.proc.stdin.write(json.dumps(payload) + "\n")   # type: ignore[union-attr]
        self.proc.stdin.flush()                             # type: ignore[union-attr]
        line = self.proc.stdout.readline()                  # type: ignore[union-attr]
        self.assertTrue(line, "server 没有回话")
        return json.loads(line)

    def test_initialize_回协议版本与服务信息(self):
        res = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["serverInfo"]["name"], "opencadstudio-mcp")
        self.assertIn("tools", res["capabilities"])

    def test_initialize_不认识的版本要回落(self):
        res = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertNotEqual(res["protocolVersion"], "1999-01-01")

    def test_tools_list_给出全部工具且_schema_完整(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(names, ["ocads_info", "ocads_run", "ocads_read", "ocads_export",
                                 "ocads_capture", "ocads_set_properties", "ocads_preview"])
        for t in tools:
            self.assertIn("description", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
        run = next(t for t in tools if t["name"] == "ocads_run")
        self.assertIn("commands", run["inputSchema"]["properties"])
        self.assertIn("engine", run["inputSchema"]["properties"])
        cap = next(t for t in tools if t["name"] == "ocads_capture")
        self.assertIn("png_path", cap["inputSchema"]["required"])

    def test_调用工具返回_content_和_structured(self):
        res = self.rpc("tools/call", {"name": "ocads_info", "arguments": {}})["result"]
        self.assertFalse(res.get("isError"))
        self.assertEqual(res["content"][0]["type"], "text")
        payload = json.loads(res["content"][0]["text"])
        self.assertIn("headless", payload)
        self.assertEqual(res["structuredContent"]["headless"], payload["headless"])

    def test_未知工具返回_isError(self):
        res = self.rpc("tools/call", {"name": "nope", "arguments": {}})["result"]
        self.assertTrue(res["isError"])

    def test_路径越界面向模型暴露为错误而不是崩溃(self):
        res = self.rpc("tools/call", {"name": "ocads_preview",
                                      "arguments": {"dxf_path": "/etc/shadow"}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("路径", res["content"][0]["text"])

    def test_未知方法返回_32601(self):
        err = self.rpc("nonsense/method")["error"]
        self.assertEqual(err["code"], -32601)

    def test_ping(self):
        self.assertEqual(self.rpc("ping")["result"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
