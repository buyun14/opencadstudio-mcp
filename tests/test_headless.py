"""集成测试：需要 OpenCADStudio 二进制。没有就整类跳过，不会误报。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocads import client, tools  # noqa: E402

os.environ.setdefault(
    "OCADS_ROOTS",
    "/public/ProjectCollection/2026_9" + os.pathsep + tempfile.gettempdir())

try:
    BIN = client.find_binary()
except client.ServeError:
    BIN = None


@unittest.skipUnless(BIN, "本机没有 OpenCADStudio 二进制，跳过")
class HeadlessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENCADSTUDIO_BIN"] = BIN  # type: ignore[assignment]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dxf = os.path.join(self.tmp.name, "t.dxf")

    def tearDown(self):
        self.tmp.cleanup()

    def test_环境自检(self):
        info = tools.info()
        self.assertTrue(info["headless"], info)
        self.assertIsNotNone(info["version"])
        self.assertIn("roots", info)

    def test_画图存图再回读一致(self):
        cmds = ["LINE 0,0 10,0", "CIRCLE 5,5 3", "PLINE 0,0 10,0 10,10 C", "DONUT 0 9 5,5"]
        res = tools.run_commands(cmds, save_path=self.dxf)
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["warnings"], [])
        self.assertTrue(os.path.isfile(self.dxf))
        back = tools.read_document("entities", open_path=self.dxf)
        self.assertEqual(back["by_type"], res["summary"])

    def test_提醒无头模式下的静默_no_op(self):
        res = tools.run_commands(["TEXT 0,0 5 0 hello", "LINE 0,0 1,1"],
                                 save_path=self.dxf)
        self.assertTrue(any("TEXT" in w for w in res["warnings"]))
        self.assertEqual(res["added"], 1)          # TEXT 静默 no-op，只有 LINE 落地了

    def test_空转命令被标记出来(self):
        # LINE 0,0 缺第二个点：上游返回 completed 但一个实体都不加 → 必须被点名
        res = tools.run_commands(["LINE 0,0", "CIRCLE 0,0 5"], save_path=self.dxf)
        self.assertIn("LINE 0,0", res["no_op"])
        self.assertEqual(res["added"], 1)

    def test_半截命令可能悄悄用退化值(self):
        # 上游不会报错：CIRCLE 0,0 缺半径也会造出一个实体 —— 所以必须回读看几何
        res = tools.run_commands(["CIRCLE 0,0"], save_path=self.dxf)
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["added"], 1)
        geom = tools.read_document("query", open_path=self.dxf,
                                   parameters={"type": "Circle", "detail": "full"})
        radius = geom["entities"][0].get("radius")
        self.assertNotEqual(radius, 5.0)          # 别信"成功"，几何才是真相

    def test_内核真交点(self):
        res = tools.run_commands(["CIRCLE 0,0 10", "LINE -20,0 20,0"], save_path=self.dxf)
        self.assertEqual(res["failed"], [])
        recs = tools.read_document("records", open_path=self.dxf,
                                   parameters={"collection": "entities"})
        handles = [r["handle"] for r in recs["records"] if r.get("handle")]
        self.assertEqual(len(handles), 2)
        hit = tools.read_document("intersections", open_path=self.dxf,
                                  parameters={"handles": handles})
        self.assertEqual(hit["count"], 2)
        xs = sorted(round(p["point"][0], 3) for p in hit["intersections"])
        self.assertEqual(xs, [-10.0, 10.0])

    def test_无头没有_measure_要明确报错(self):
        res = tools.call_tool("ocads_read", {"op": "measure", "open_path": self.dxf})
        self.assertFalse(res["ok"])
        self.assertIn("measure", res["error"])

    def test_导出_dwg(self):
        tools.run_commands(["CIRCLE 0,0 5"], save_path=self.dxf)
        dst = os.path.join(self.tmp.name, "t.dwg")
        res = tools.export(self.dxf, dst)
        self.assertTrue(res["ok"], res)
        self.assertGreater(res["bytes"], 1000)

    def test_预览读的是文件本身(self):
        tools.run_commands(["SPLINE 0,0 20,10 40,0 C", "CIRCLE 20,8 4"],
                           save_path=self.dxf)
        png = os.path.join(self.tmp.name, "t.png")
        res = tools.preview(self.dxf, png, scale=6)
        self.assertTrue(os.path.isfile(png))
        self.assertEqual(res["drawn"], res["entities"])
        self.assertEqual(res["skipped"], 0)

    def test_路径沙箱拦住越界写入(self):
        with self.assertRaises(tools.ToolError):
            tools.resolve_path("/etc/shadow")
        with self.assertRaises(tools.ToolError):
            tools.resolve_path(os.path.join(self.tmp.name, "x.pdf"), suffix=".dxf")

    def test_导出格式白名单(self):
        tools.run_commands(["CIRCLE 0,0 5"], save_path=self.dxf)
        with self.assertRaises(tools.ToolError):
            tools.export(self.dxf, os.path.join(self.tmp.name, "t.pdf"))

    def test_视图名校验在起进程之前(self):
        res = tools.call_tool("ocads_set_view", {"name": "isometric"})
        self.assertFalse(res["ok"])
        self.assertIn("home/extents", res["error"])

    def test_未知工具报错(self):
        self.assertEqual(tools.call_tool("ocads_nope", {})["ok"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
