#!/usr/bin/env python3
"""示例：用 ocads 画一只海獭（小心海），并出图验收。

跑法：

    OPENCADSTUDIO_BIN=/path/to/OpenCADStudio python3 examples/otter_draw.py [输出目录]

流程就是 SKILL 里那套：生成 → 回读比对 → 出图。坐标纯手工算，
想改姿势直接改下面的常量。
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocads import tools  # noqa: E402

out_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ocads-otter"
os.makedirs(out_dir, exist_ok=True)

cmds: list[str] = []


def f(p):
    return f"{p[0]:.4f},{p[1]:.4f}"


def line(a, b):
    cmds.append(f"LINE {f(a)} {f(b)}")


def circle(c, r):
    cmds.append(f"CIRCLE {f(c)} {r:.4f}")


def ellipse(c, axis_end, other):
    cmds.append(f"ELLIPSE {f(c)} {f(axis_end)} {other:.4f}")


def arc3(a, b, c):
    cmds.append(f"ARC {f(a)} {f(b)} {f(c)}")


def spline(pts, close=False):
    cmds.append("SPLINE " + " ".join(f(p) for p in pts) + (" C" if close else ""))


def donut(outer, c):
    cmds.append(f"DONUT 0 {outer:.4f} {f(c)}")


def rect(a, b):
    cmds.append(f"RECTANG {f(a)} {f(b)}")


def mx(p):
    return (-p[0], p[1])


def wavy(x, y0=-8.0):
    return (x, y0 + 2.5 * math.sin(x / 13.0))


# 轮廓：右半边从下往上，再镜像闭合
RIGHT = [(0, 0), (12, 1), (24, 7), (33, 18), (39, 32), (41, 46), (39, 58),
         (34, 68), (29, 76), (27, 84), (30, 90), (33, 97), (32, 105),
         (34, 111), (32, 117), (26, 122), (17, 126), (7, 128), (0, 129)]
spline(RIGHT + [mx(p) for p in reversed(RIGHT[1:-1])], close=True)

for sx in (1, -1):                                    # 耳内
    arc3((27 * sx, 116), (31 * sx, 112), (26 * sx, 108))
for sx in (1, -1):                                    # 眼睛（DONUT 0 N → 视觉半径 N/2）
    donut(9, (13 * sx, 96))
    circle((14.6 * sx, 97.6), 1.2)
donut(10, (0, 84))                                    # 鼻子
arc3((-1, 79), (-6, 75), (-10, 76))                   # 嘴
arc3((1, 79), (6, 75), (10, 76))
for sx in (1, -1):                                    # 胡须
    line((16 * sx, 92), (38 * sx, 99))
    line((17 * sx, 88), (40 * sx, 88))
    line((16 * sx, 84), (38 * sx, 78))
for sx in (1, -1):                                    # 爪 / 脚
    ellipse((16 * sx, 48), (8 * sx, 58), 5.0)
    ellipse((16 * sx, 8), (26 * sx, 8), 5.5)
    line((23 * sx, 12), (28 * sx, 15))
    line((25 * sx, 7), (30 * sx, 5))
ellipse((0, -4), (26, -4), 5.0)                       # 尾巴
arc3((-11, 60), (0, 71), (11, 60))                    # 蛤蜊壳
line((-11, 60), (11, 60))
line((0, 60), (0, 71))
line((0, 60), (-7.78, 67.78))
line((0, 60), (7.78, 67.78))
spline([(x, -8 + 2.5 * math.sin(x / 13.0)) for x in [-84 + i * 8.4 for i in range(21)]])
spline([(x, -24 + 2.5 * math.sin(x / 13.0)) for x in [-84 + i * 9.0 for i in range(13)]])
for sx in (1, -1):                                    # 浪尖
    arc3(wavy(45 * sx), (41 * sx, -3), wavy(37 * sx))
arc3((-53, -38), (-46, -31), (-39, -38))              # 海底小贝壳
line((-53, -38), (-39, -38))
for dx in (-4.4, 0, 4.4):
    line((-46, -38), (-46 + dx, -38 + math.sqrt(max(49 - dx * dx, 0))))
rect((-88, -48), (88, 142))                           # 图框
rect((-84, -44), (84, 138))
rect((26, -44), (84, -24))
line((26, -34), (84, -34))
line((58, -44), (58, -24))


def main() -> int:
    dxf = os.path.join(out_dir, "otter.dxf")
    png = os.path.join(out_dir, "otter.png")

    drawn = tools.run_commands(cmds, save_path=dxf)
    print(f"命令 {drawn['commands']} 条，成功 {drawn['commands'] - len(drawn['failed'])} 条，"
          f"新增实体 {drawn['added']}")
    if drawn["failed"]:
        print("失败命令:", drawn["failed"])
    if drawn["warnings"]:
        print("警告:", drawn["warnings"])

    back = tools.read_document("entities", open_path=dxf)
    print("回读比对:", back.get("by_type"), "总数", back.get("total", back.get("count")))
    if drawn["summary"] != back.get("by_type"):
        print("!! 生成统计与回读不一致，别急着交付")
        return 2

    shot = tools.preview(dxf, png, colors={"SPLINE": "black", "ARC": "black"})
    print(f"预览 {shot['png']} {shot['pixel_size']} 画了 {shot['drawn']} 个实体，"
          f"跳过 {shot['skipped']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
