"""mcp 引擎（上游 GUI 控制面）测试：需要二进制 + xvfb/DISPLAY，缺了就整类跳过。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocads import client, mcp_client, tools  # noqa: E402

os.environ.setdefault("OCADS_ROOTS", tempfile.gettempdir())

try:
    BIN = client.find_binary()
except client.ServeError:
    BIN = None

CAN_RUN = bool(BIN) and bool(os.environ.get("DISPLAY") or shutil.which("xvfb-run"))


@unittest.skipUnless(CAN_RUN, "没有二进制，或既无 DISPLAY 也无 xvfb-run")
class McpEngineTest(unittest.TestCase):
    """每个用例起一个编辑器实例（xvfb-run + --mcp），所以用例别太多。"""

    @classmethod
    def setUpClass(cls):
        os.environ["OPENCADSTUDIO_BIN"] = BIN  # type: ignore[assignment]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_弹窗清掉后能建出图纸(self):
        with mcp_client.UpstreamMCP(timeout=180) as m:
            prep = m.prepare_document()
            self.assertIsNotNone(prep["document"], "new 之后应该有 document_id")
            self.assertIsNone(prep["modals"]["left"], f"还有弹窗没处理：{prep['modals']}")
            # 关过的弹窗只能是白名单里的
            for name in prep["modals"]["closed"]:
                self.assertIn(name, mcp_client.MODAL_ALLOWLIST)

    def test_画图_量测_截图一条龙(self):
        png = os.path.join(self.tmp.name, "shot.png")
        res = tools.capture(png, commands=["CIRCLE 0,0 10", "LINE -20,0 20,0"])
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["entities"], 2)
        self.assertTrue(os.path.isfile(png))
        self.assertGreater(res["bytes"], 3000, "截图太小，八成是空白或报错图")

    def test_量测给的是内核真值(self):
        r = tools.run_commands(["CIRCLE 0,0 10"], engine="mcp")
        self.assertEqual(r["failed"], [])
        self.assertTrue(r.get("handles"), "应该拿得到句柄")
        ms = (r.get("measurements") or {}).get("measurements") or []
        self.assertTrue(ms, f"measure 没回数据：{str(r.get('measurements'))[:200]}")
        curve = ms[0].get("curve") or {}
        self.assertAlmostEqual(curve.get("length", 0), 62.83, places=1)   # 2πr
        self.assertTrue(curve.get("closed"))

    def test_文字走交互步骤能建出来(self):
        # run 的批处理对 TEXT 是静默 no-op，只有 start/input + text_input/text_commit 这条路行
        with mcp_client.UpstreamMCP(timeout=180) as m:
            prep = m.prepare_document()
            sid, did = prep["session"], prep["document"]
            m.run_many(["CIRCLE 0,0 10"], did, session=sid)
            out = m.add_text("OCADS", (0, 20), 5.0, 0.0, document=did, session=sid)
            self.assertEqual(out["texts"], 1, f"文字没建出来：{out}")

    def test_view_home_是可靠的重置视图方式(self):
        with mcp_client.UpstreamMCP(timeout=180) as m:
            prep = m.prepare_document()
            sid, did = prep["session"], prep["document"]
            m.run_many(["CIRCLE 0,0 10"], did, session=sid)
            res = m.call("action", session=sid, document=did, name="view_home")
            self.assertIn(res.get("status"), ("completed", "ok"))

    def test_改属性落在数据上(self):
        with mcp_client.UpstreamMCP(timeout=180) as m:
            prep = m.prepare_document()
            sid, did = prep["session"], prep["document"]
            self.assertEqual(m.run_many(["CIRCLE 0,0 5"], did, session=sid)["failed"], [])
            handle = m.handles(did, session=sid)[0]
            res = m.set_properties(handle, [{"path": "/common/color", "value": {"Index": 1}}],
                                   did, session=sid)
            self.assertEqual(res.get("status"), "completed", res)
            recs = m.read("records", session=sid, document=did,
                          parameters={"collection": "entities"})
            color = recs["records"][0]["properties"]["common"]["color"]
            self.assertEqual(color, {"Index": 1}, f"颜色没写进去：{color!r}")

    def test_清理不留孤儿进程(self):
        before = _count("Xvfb")
        with mcp_client.UpstreamMCP(timeout=180) as m:
            m.prepare_document()
        import time
        time.sleep(2)
        self.assertLessEqual(_count("Xvfb"), before, "close() 之后不该多出 Xvfb 孤儿")


def _count(name: str) -> int:
    import subprocess
    out = subprocess.run(["pgrep", "-xc", name], capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
