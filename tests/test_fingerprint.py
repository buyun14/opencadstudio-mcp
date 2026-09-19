"""指纹（if_changed）单元测试：纯本地，不需要 CAD 二进制。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from ocads import fingerprint as fp  # noqa: E402


def make(path: str, *, size=(400, 300), lines=(), extra_pixel=None):
    img = Image.new("RGB", size, (255, 255, 255))
    dr = ImageDraw.Draw(img)
    for coords in lines:
        dr.line(coords, fill=(0, 0, 0), width=1)
    if extra_pixel:
        img.putpixel(extra_pixel, (0, 0, 0))
    img.save(path)
    return path


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = os.path.join(self.tmp.name, "a.png")
        self.b = os.path.join(self.tmp.name, "b.png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_第一次截图算基线(self):
        make(self.a)
        res = fp.check(self.a)
        self.assertTrue(res["baseline"])
        self.assertTrue(res["changed"])

    def test_完全一样判定未变(self):
        make(self.a, lines=[((0, 0), (399, 299))])
        fp.check(self.a)
        res = fp.check(self.a)
        self.assertFalse(res["changed"], res)
        self.assertEqual(res["ratio"], 0.0)

    def test_多一根细线必须被认出来(self):
        """这是设计要点：1px 线在 400x300 图上平均下来极小，用平均值会漏判。"""
        make(self.a)
        fp.check(self.a)
        make(self.a, lines=[((10, 150), (390, 150))])       # 同路径覆盖：加一根横线
        res = fp.check(self.a)
        self.assertTrue(res["changed"], res)
        self.assertGreater(res["ratio"], 0.005)

    def test_小于阈值的小改动可放过(self):
        make(self.a)
        fp.check(self.a)
        make(self.a, extra_pixel=(200, 150))                # 一个像素
        res = fp.check(self.a, threshold=0.005)
        self.assertFalse(res["changed"], res)

    def test_尺寸变化一律算变(self):
        make(self.a, size=(400, 300))
        fp.check(self.a)
        make(self.b, size=(800, 600))
        res = fp.check(self.b)
        self.assertTrue(res["changed"])
        self.assertEqual(res["ratio"], 1.0)

    def test_阈值可调(self):
        make(self.a, lines=[((0, 0), (399, 0))])            # 顶部一根线
        fp.check(self.a)                                    # 基线
        make(self.a, lines=[((0, 0), (399, 0)), ((0, 299), (399, 299))])
        # update=False：两次比较都对同一条基线，避免前一次把基线改掉
        loose = fp.check(self.a, threshold=0.5, update=False)
        strict = fp.check(self.a, threshold=0.0, update=False)
        self.assertFalse(loose["changed"], loose)
        self.assertTrue(strict["changed"], strict)

    def test_坏图报错而不是静默(self):
        bad = os.path.join(self.tmp.name, "bad.png")
        with open(bad, "w") as fh:
            fh.write("not an image")
        with self.assertRaises(fp.FingerprintError):
            fp.signature(bad)

    def test_来源变了就算基线不算没变(self):
        """同一路径换了另一份内容（比如另一次捕获），不能因为长得像就判"没变"。"""
        make(self.a)
        fp.check(self.a, source="doc1")
        res = fp.check(self.a, source="doc2")
        self.assertTrue(res["baseline"])
        self.assertTrue(res["changed"])

    def test_未变时不更新基线避免漂移(self):
        """一串"每次差一点点"的微改不该让基线越漂越远。"""
        make(self.a, lines=[((0, 0), (399, 0))])
        fp.check(self.a, source="s")                # 基线
        for offset in (60, 120, 180):
            make(self.a, lines=[((0, 0), (399, 0)), ((0, offset), (399, offset))])
            first = fp.check(self.a, source="s")
            if first["changed"]:
                break
        else:
            self.fail("连续微改一直判未变，基线漂移了")
        # 判定有变之后，基线才应该跟上去
        again = fp.check(self.a, source="s")
        self.assertFalse(again["changed"], again)

    def test_写不出指纹文件也不该炸(self):
        make(self.a)
        # 把 sidecar 路径变成目录名，制造写失败
        import os as _os
        _os.makedirs(fp.sidecar_path(self.a), exist_ok=True)
        res = fp.check(self.a)
        self.assertTrue(res["changed"])
        self.assertIn("fingerprint_warning", res)

    def test_格子数必须钉住(self):
        """tobytes() 是 1 字节/像素这个前提要钉住：哪天变成 4 字节，自比仍会"通过"但比较是错的。"""
        make(self.a)
        for grid in (8, 12, 16):
            sig = fp.signature(self.a, grid=grid)
            self.assertEqual(len(sig["tiles"]), grid * grid)
            self.assertEqual(sig["grid"], grid)

    def test_透明图不该把透明区当墨迹(self):
        img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))       # 全透明
        img.save(self.a)
        self.assertEqual(fp.signature(self.a)["ink"], 0.0)

    def test_不给来源时和带来源的基线不互认(self):
        make(self.a)
        fp.check(self.a, source="doc1")
        res = fp.check(self.a)                                  # source=None
        self.assertTrue(res["baseline"], res)

    def test_指纹文件跟着图走(self):
        make(self.a)
        fp.check(self.a)
        self.assertTrue(os.path.isfile(fp.sidecar_path(self.a)))
        self.assertEqual(fp.sidecar_path("/x/y.png"), "/x/y.png.sig.json")


class WrapperTest(unittest.TestCase):
    """tools._fingerprint 包装：指纹失败不许把截图结果带崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = os.path.join(self.tmp.name, "x.png")
        make(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_指纹异常降级为字段(self):
        from unittest import mock

        from ocads import tools
        with mock.patch.object(tools._fp, "check", side_effect=tools._fp.FingerprintError("坏了")):
            res = tools._fingerprint(self.png, if_changed=True, threshold=0.005, source="s")
        self.assertIn("fingerprint_error", res)
        self.assertNotIn("changed", res)

    def test_缺_Pillow_也降级(self):
        from unittest import mock

        from ocads import tools
        with mock.patch.object(tools._fp, "check", side_effect=ImportError("No module named PIL")):
            res = tools._fingerprint(self.png, if_changed=True, threshold=0.005, source="s")
        self.assertIn("fingerprint_error", res)

    def test_未变时给复用提示(self):
        from ocads import tools
        tools._fingerprint(self.png, if_changed=True, threshold=0.005, source="s")   # 基线
        res = tools._fingerprint(self.png, if_changed=True, threshold=0.005, source="s")
        self.assertFalse(res["changed"])
        self.assertEqual(res["reuse_previous"], self.png)
        self.assertIn("note", res)

    def test_关闭开关时不产生任何字段(self):
        from ocads import tools
        self.assertEqual(tools._fingerprint(self.png, if_changed=False, threshold=0.005, source="s"), {})

    def test_入口契约覆盖指纹异常(self):
        from ocads import tools
        res = tools.call_tool("ocads_preview", {"dxf_path": self.png})       # 不是 DXF
        self.assertFalse(res["ok"])
        self.assertIn("error", res)

    def test_未知工具仍是结构化错误(self):
        from ocads import tools
        self.assertFalse(tools.call_tool("ocads_nope", {})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
