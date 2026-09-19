"""纯逻辑测试：不需要 OpenCADStudio 二进制，只要有 Pillow。"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocads import dxf  # noqa: E402

FIXTURE = """0
SECTION
2
TABLES
0
TABLE
2
LAYER
0
LAYER
2
0
62
7
0
LAYER
2
A
62
8
0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
8
0
10
0.0
20
0.0
11
10.0
21
0.0
0
CIRCLE
8
A
10
5.0
20
5.0
40
2.5
0
LWPOLYLINE
8
0
90
2
70
1
43
4.0
10
0.0
20
0.0
42
1.0
10
4.0
20
0.0
42
1.0
0
ELLIPSE
8
0
10
0.0
20
0.0
11
10.0
21
0.0
40
0.5
41
0.0
42
6.283185307179586
0
ARC
8
0
10
3.0
20
3.0
40
2.0
50
0.0
51
180.0
0
SPLINE
8
0
70
0
71
3
74
4
11
0.0
21
0.0
31
0.0
11
5.0
21
5.0
31
0.0
11
10.0
21
0.0
31
0.0
11
15.0
21
0.0
31
0.0
0
LEADER
8
0
0
ENDSEC
0
EOF
"""


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fixture.dxf")
        with open(self.path, "w") as fh:
            fh.write(FIXTURE)
        self.parsed = dxf.parse(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_解析出全部实体类型(self):
        stats = dxf.summarize(self.parsed)
        self.assertEqual(stats["entities"], 7)
        for kind in ("LINE", "CIRCLE", "LWPOLYLINE", "ELLIPSE", "ARC", "SPLINE", "LEADER"):
            self.assertEqual(stats["by_type"].get(kind), 1, kind)
        self.assertEqual(stats["layers"], ["0", "A"])

    def test_图层的颜色被读出来(self):
        self.assertEqual(self.parsed["layers"]["A"]["color"], 8)
        self.assertEqual(self.parsed["header"], {})

    def test_未知实体计入_skipped_而不是静默消失(self):
        self.assertEqual(dxf.summarize(self.parsed)["skipped"], 1)

    def test_bulge_半圆落在弦的另一侧(self):
        poly = next(e for e in self.parsed["entities"] if e["type"] == "LWPOLYLINE")
        pts = dxf.polyline_of(poly)
        self.assertGreater(len(pts), 10)
        # 两个顶点都必须是实心盘（DONUT 0 4）的圆心连线，中点下方的半圆半径 2
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.assertAlmostEqual(min(xs), 0.0, places=3)
        self.assertAlmostEqual(max(xs), 4.0, places=3)
        self.assertAlmostEqual(min(ys), -2.0, places=2)
        self.assertAlmostEqual(max(ys), 0.0, places=2)

    def test_椭圆比率与轴向(self):
        ell = next(e for e in self.parsed["entities"] if e["type"] == "ELLIPSE")
        pts = dxf.polyline_of(ell)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.assertAlmostEqual(max(xs), 10.0, places=2)
        self.assertAlmostEqual(max(ys), 5.0, places=2)   # 10 * ratio 0.5

    def test_弧的角度与方向(self):
        arc = next(e for e in self.parsed["entities"] if e["type"] == "ARC")
        pts = dxf.polyline_of(arc)
        self.assertAlmostEqual(pts[0][0], 5.0, places=3)   # 0° → 圆心右边
        self.assertAlmostEqual(pts[-1][0], 1.0, places=3)  # 180° → 圆心左边
        self.assertAlmostEqual(max(p[1] for p in pts), 5.0, places=2)

    def test_样条穿过拟合点(self):
        spl = next(e for e in self.parsed["entities"] if e["type"] == "SPLINE")
        pts = dxf.polyline_of(spl)
        self.assertGreater(len(pts), 20)
        # 曲线必须穿过拟合点（端点精确），段内允许插值带来的少量起伏
        self.assertAlmostEqual(min(p[0] for p in pts), 0.0, places=2)
        self.assertAlmostEqual(max(p[0] for p in pts), 15.0, places=2)
        self.assertAlmostEqual(max(p[1] for p in pts), 5.0, places=2)
        self.assertGreater(min(p[1] for p in pts), -1.0)
        self.assertLess(min(p[1] for p in pts), 0.0)

    def test_渲染出图(self):
        out = os.path.join(self.tmp.name, "shot.png")
        res = dxf.render(self.path, out, scale=10, colors={"CIRCLE": "red"})
        self.assertTrue(os.path.isfile(out))
        self.assertGreater(res["bytes"], 500)
        self.assertEqual(res["drawn"], 6)          # LEADER 画不出来
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["by_type"]["LINE"], 1)

    def test_空图纸要报错而不是出一张空白图(self):
        empty = os.path.join(self.tmp.name, "empty.dxf")
        with open(empty, "w") as fh:
            fh.write("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
        with self.assertRaises(dxf.DxfError):
            dxf.render(empty, os.path.join(self.tmp.name, "empty.png"))

    def test_颜色表(self):
        self.assertEqual(dxf.aci_rgb(7), (0, 0, 0))
        self.assertEqual(dxf.aci_rgb(None), (0, 0, 0))
        self.assertEqual(len(dxf.aci_rgb(3)), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
