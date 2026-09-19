"""DXF 读取 + 自绘渲染（Pillow）。

为什么要自带渲染器：OpenCADStudio 的无头 ``--export`` 只支持 ``.dwg/.dxf``，
``pdf_export.rs`` 那条路只被 GUI 的 plot/print 调用。所以"看图"这一步要么靠
带显示的机器，要么自己把 DXF 画出来。这个模块走后者，好处是**渲染的是文件
本身**（而不是生成时的内存几何），能当验真手段用。

支持 OpenCADStudio 实际写出的实体：LINE / CIRCLE / ARC / ELLIPSE / LWPOLYLINE /
POLYLINE+VERTEX / SPLINE（拟合点或控制点）/ SOLID / POINT / TEXT / MTEXT。
不认识的实体不会静默消失——统计在 ``skipped`` 里返回。
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable

__all__ = ["DxfError", "parse", "render", "summarize", "polyline_of", "aci_rgb"]

_SKIP_TYPES = {"SEQEND", "ATTRIB", "ATTDEF", "VIEWPORT", "MLINE"}


class DxfError(RuntimeError):
    """DXF 读不了或画不了。"""


# ------------------------------------------------------------------ 颜色（ACI 精简表）
_ACI = {
    1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
    5: (0, 0, 255), 6: (255, 0, 255), 7: (0, 0, 0), 8: (128, 128, 128),
    9: (192, 192, 192), 30: (255, 127, 0), 40: (255, 191, 0), 50: (191, 255, 0),
    90: (0, 255, 127), 130: (0, 191, 255), 150: (0, 63, 255), 190: (127, 0, 255),
    210: (255, 0, 191), 250: (51, 51, 51), 251: (80, 80, 80), 252: (102, 102, 102),
    253: (153, 153, 153), 254: (204, 204, 204), 255: (255, 255, 255),
}


def aci_rgb(index: int | None) -> tuple[int, int, int]:
    """AutoCAD 颜色索引 → RGB（近似表，够看就行）。"""
    if index is None:
        return (0, 0, 0)
    return _ACI.get(index, (0, 0, 0))


# ------------------------------------------------------------------ 解析
def _read_pairs(path: str) -> list[tuple[str, str]]:
    last_error: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "cp936", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                raw = fh.read()
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise DxfError(f"读不出文本编码：{path}（{last_error}）")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 4:
        raise DxfError(f"文件太短，不像 DXF：{path}")
    return [(lines[i].strip(), lines[i + 1].strip()) for i in range(0, len(lines) - 1, 2)]


def _num(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise DxfError(f"该是数字的地方出现了 {value!r}") from None


def parse(path: str) -> dict[str, Any]:
    """解析 DXF：HEADER 变量、LAYER 表、ENTITIES 段。"""
    if not os.path.isfile(path):
        raise DxfError(f"图纸不存在：{path}")
    pairs = _read_pairs(path)

    header: dict[str, str] = {}
    layers: dict[str, dict[str, Any]] = {}
    entities: list[dict[str, Any]] = []

    section: str | None = None
    header_var: str | None = None
    cur_layer: dict[str, Any] | None = None
    cur_entity: dict[str, Any] | None = None

    def flush_entity() -> None:
        nonlocal cur_entity
        if cur_entity is not None:
            entities.append(cur_entity)
            cur_entity = None

    def flush_layer() -> None:
        nonlocal cur_layer
        if cur_layer is not None:
            layers[cur_layer.get("name", "0")] = cur_layer
            cur_layer = None

    for code, value in pairs:
        if code == "0" and value == "SECTION":
            section = "?"
            continue
        if section == "?" and code == "2":
            section = value
            continue
        if code == "0" and value == "ENDSEC":
            flush_entity()
            flush_layer()
            section = None
            continue

        if section == "HEADER":
            if code == "9":
                header_var = value
                header.setdefault(value, "")
            elif header_var:
                header[header_var] = value
            continue

        if section == "TABLES":
            if code == "0":
                flush_layer()
                if value == "LAYER":
                    cur_layer = {"name": "0", "color": 7}
                continue
            if cur_layer is not None:
                if code == "2":
                    cur_layer["name"] = value
                elif code == "62":
                    cur_layer["color"] = int(_num(value))
                elif code == "70":
                    cur_layer["flags"] = int(_num(value))
                continue
            continue

        if section != "ENTITIES":
            continue

        if code == "0":
            flush_entity()
            if value in _SKIP_TYPES:
                cur_entity = {"type": value, "_skip": True}
            else:
                cur_entity = {"type": value, "_": []}
            continue
        if cur_entity is not None and not cur_entity.get("_skip"):
            cur_entity["_"].append((code, value))

    flush_entity()
    flush_layer()
    return {"path": os.path.abspath(path), "header": header, "layers": layers,
            "entities": [e for e in entities if not e.get("_skip")]}


# ------------------------------------------------------------------ 组码 → 属性
def props(entity: dict[str, Any]) -> dict[str, Any]:
    """把组码列表折叠成字典。注意组码含义依赖实体类型（40 是半径还是比率）。"""
    et = entity["type"]
    out: dict[str, Any] = {"vertices": [], "control": [], "fit": [], "text": [], "colors": []}
    vert: dict[str, float] | None = None

    _POINT_KEY = {"10": "p", "20": "p", "30": "p", "11": "p2", "21": "p2", "31": "p2",
                  "12": "p3", "22": "p3", "32": "p3", "13": "p4", "23": "p4", "33": "p4"}

    def point(code: str, value: str) -> None:
        out.setdefault(_POINT_KEY[code], {})[_AXIS[code]] = _num(value)

    _AXIS = {"10": "x", "20": "y", "30": "z", "11": "x", "21": "y", "31": "z",
             "12": "x", "22": "y", "32": "z", "13": "x", "23": "y", "33": "z"}

    for code, value in entity.get("_", []):
        if et == "LWPOLYLINE":
            if code == "10":
                vert = {"x": _num(value)}
                out["vertices"].append(vert)
            elif code in ("20", "40", "41", "42") and vert is not None:
                vert[{"20": "y", "40": "w0", "41": "w1", "42": "bulge"}[code]] = _num(value)
            elif code == "70":
                out["flags"] = int(_num(value))
            elif code == "43":
                out["const_width"] = _num(value)
            elif code == "8":
                out["layer"] = value
            elif code == "62":
                out["color"] = int(_num(value))
            elif code == "5":
                out["handle"] = value
            continue
        if et == "VERTEX":
            if code == "10":
                vert = {"x": _num(value)}
                out["vertices"].append(vert)
            elif code in ("20", "42") and vert is not None:
                vert[{"20": "y", "42": "bulge"}[code]] = _num(value)
            continue
        if et == "SPLINE":
            if code == "10":
                out["control"].append({"x": _num(value)})
            elif code == "20" and out["control"] and "y" not in out["control"][-1]:
                out["control"][-1]["y"] = _num(value)
            elif code == "11":
                out["fit"].append({"x": _num(value)})
            elif code == "21" and out["fit"] and "y" not in out["fit"][-1]:
                out["fit"][-1]["y"] = _num(value)
            elif code in ("70", "71", "72", "73", "74"):
                out[{"70": "flags", "71": "degree", "72": "knot_count",
                     "73": "ctrl_count", "74": "fit_count"}[code]] = int(_num(value))
            elif code == "8":
                out["layer"] = value
            elif code == "62":
                out["color"] = int(_num(value))
            elif code == "5":
                out["handle"] = value
            continue
        if et == "ELLIPSE":
            if code in ("10", "11"):
                point(code, value)
            elif code == "40":
                out["ratio"] = _num(value)
            elif code == "41":
                out["start_param"] = _num(value)
            elif code == "42":
                out["end_param"] = _num(value)
            elif code == "8":
                out["layer"] = value
            elif code == "62":
                out["color"] = int(_num(value))
            elif code == "5":
                out["handle"] = value
            continue
        if et in ("TEXT", "MTEXT"):
            if code == "10":
                point(code, value)
            elif code == "40":
                out["height"] = _num(value)
            elif code == "50":
                out["rotation"] = _num(value)
            elif code in ("1", "3"):
                out["text"].append(value)
            elif code == "8":
                out["layer"] = value
            elif code == "62":
                out["color"] = int(_num(value))
            elif code == "5":
                out["handle"] = value
            continue
        # 其余实体（LINE/CIRCLE/ARC/SOLID/POINT…）走通用映射
        if code in _POINT_KEY:
            point(code, value)
        elif code == "40":
            out["radius"] = _num(value)
        elif code == "50":
            out["start_angle"] = _num(value)
        elif code == "51":
            out["end_angle"] = _num(value)
        elif code == "8":
            out["layer"] = value
        elif code == "62":
            out["color"] = int(_num(value))
        elif code == "5":
            out["handle"] = value
        elif code == "70":
            out["flags"] = int(_num(value))
    return out


# ------------------------------------------------------------------ 采样
def _arc_pts(cx, cy, radius, a0, a1, steps: int = 72):
    if a1 <= a0:
        a1 += 360.0
    return [(cx + radius * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + radius * math.sin(math.radians(a0 + (a1 - a0) * i / steps)))
            for i in range(steps + 1)]


def _bulge_pts(p0, p1, bulge: float, steps: int = 24):
    """LWPOLYLINE 的凸度段：bulge = tan(θ/4)，正=逆时针。"""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    if not bulge or chord == 0:
        return [p0, p1]
    theta = 4 * math.atan(bulge)
    half = theta / 2
    if abs(math.tan(half)) < 1e-12:                # θ≈2π，退化成整圆
        return _arc_pts((x0 + x1) / 2, (y0 + y1) / 2, chord / 2, 0, 360)
    dist = (chord / 2) / math.tan(half)
    nx, ny = -dy / chord, dx / chord               # 弦的左法线
    cx, cy = (x0 + x1) / 2 + dist * nx, (y0 + y1) / 2 + dist * ny
    radius = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    return [(cx + radius * math.cos(a0 + theta * i / steps),
             cy + radius * math.sin(a0 + theta * i / steps)) for i in range(steps + 1)]


def _ellipse_pts(cx, cy, ax, ay, ratio, t0, t1, steps: int = 96):
    major = math.hypot(ax, ay) or 1.0
    minor = major * (ratio or 1.0)
    rot = math.atan2(ay, ax)
    if t1 <= t0:
        t1 += 2 * math.pi
    out = []
    for i in range(steps + 1):
        t = t0 + (t1 - t0) * i / steps
        x, y = major * math.cos(t), minor * math.sin(t)
        out.append((cx + x * math.cos(rot) - y * math.sin(rot),
                    cy + x * math.sin(rot) + y * math.cos(rot)))
    return out


def _catmull(pts, closed: bool, per_seg: int = 16):
    """样条插值：向心 Catmull-Rom（α=0.5，抑制过冲），端点用反射延拓。

    OCS 写出的 ``SPLINE`` 是"曲线过拟合点"的语义，所以这里必须真的穿过给定点。
    这不是精确 NURBS 求值——预览够用，跟真 CAGD 内核求出来的会有细微差别。
    """
    n = len(pts)
    if n < 3:
        return list(pts)
    if not closed:
        head = (2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])
        tail = (2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1])
        seq = [head, *pts, tail]
        segments = [(seq[i - 1], seq[i], seq[i + 1], seq[i + 2])
                    for i in range(1, len(seq) - 2)]
    else:
        seq = list(pts)
        segments = [(seq[(i - 1) % n], seq[i], seq[(i + 1) % n], seq[(i + 2) % n])
                    for i in range(n)]

    out: list[tuple[float, float]] = []
    for p0, p1, p2, p3 in segments:
        t0 = 0.0
        t1 = t0 + max(math.hypot(p1[0] - p0[0], p1[1] - p0[1]) ** 0.5, 1e-6)
        t2 = t1 + max(math.hypot(p2[0] - p1[0], p2[1] - p1[1]) ** 0.5, 1e-6)
        t3 = t2 + max(math.hypot(p3[0] - p2[0], p3[1] - p2[1]) ** 0.5, 1e-6)

        def blend(a, b, w0, w1):
            return ((a[0] * w0 + b[0] * w1), (a[1] * w0 + b[1] * w1))

        for k in range(per_seg):
            t = t1 + (t2 - t1) * k / per_seg
            a1 = blend(p0, p1, (t1 - t) / (t1 - t0), (t - t0) / (t1 - t0))
            a2 = blend(p1, p2, (t2 - t) / (t2 - t1), (t - t1) / (t2 - t1))
            a3 = blend(p2, p3, (t3 - t) / (t3 - t2), (t - t2) / (t3 - t2))
            b1 = blend(a1, a2, (t2 - t) / (t2 - t0), (t - t0) / (t2 - t0))
            b2 = blend(a2, a3, (t3 - t) / (t3 - t1), (t - t1) / (t3 - t1))
            out.append(blend(b1, b2, (t2 - t) / (t2 - t1), (t - t1) / (t2 - t1)))
    if closed:
        out.append(out[0])
    else:
        out.append(pts[-1])
    return out


def polyline_of(entity: dict[str, Any]) -> list[tuple[float, float]]:
    """把一个实体采样成折线（画不出来的返回空列表）。"""
    et = entity["type"]
    p = props(entity)
    pts: list[tuple[float, float]] = []

    if et == "LINE":
        return [_pt(p, "p"), _pt(p, "p2")] if p.get("p") and p.get("p2") else []
    if et == "CIRCLE":
        c = p.get("p", {})
        return _arc_pts(c.get("x", 0.0), c.get("y", 0.0), p.get("radius", 0.0), 0, 360)
    if et == "ARC":
        c = p.get("p", {})
        return _arc_pts(c.get("x", 0.0), c.get("y", 0.0), p.get("radius", 0.0),
                        p.get("start_angle", 0.0), p.get("end_angle", 360.0))
    if et == "ELLIPSE":
        c, e = p.get("p", {}), p.get("p2", {})
        return _ellipse_pts(c.get("x", 0.0), c.get("y", 0.0), e.get("x", 0.0), e.get("y", 0.0),
                            p.get("ratio", 1.0), p.get("start_param", 0.0),
                            p.get("end_param", 2 * math.pi))
    if et in ("LWPOLYLINE", "POLYLINE"):
        verts = [v for v in p.get("vertices", []) if "x" in v]
        for i, v in enumerate(verts):
            nxt = verts[i + 1] if i + 1 < len(verts) else None
            if nxt and nxt is not None and v.get("bulge"):
                pts += _bulge_pts((v["x"], v.get("y", 0.0)), (nxt["x"], nxt.get("y", 0.0)),
                                  v["bulge"])[:-1]
            else:
                pts.append((v["x"], v.get("y", 0.0)))
        if et == "LWPOLYLINE" and p.get("flags", 0) & 1 and pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts
    if et == "SPLINE":
        fit = [(v["x"], v.get("y", 0.0)) for v in p.get("fit", []) if "x" in v]
        ctrl = [(v["x"], v.get("y", 0.0)) for v in p.get("control", []) if "x" in v]
        closed = bool(p.get("flags", 0) & 1)
        return _catmull(fit or ctrl, closed)
    if et in ("SOLID", "3DFACE"):
        pts = [_pt(p, k) for k in ("p", "p2", "p3", "p4") if p.get(k)]
        return pts + [pts[0]] if len(pts) >= 2 else []
    return []


def _pt(p: dict[str, Any], key: str) -> tuple[float, float]:
    d = p.get(key) or {}
    return (d.get("x", 0.0), d.get("y", 0.0))


def summarize(parsed: dict[str, Any]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    xs: list[float] = []
    ys: list[float] = []
    skipped = 0
    for e in parsed.get("entities", []):
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        seg = polyline_of(e)
        if e["type"] not in ("POINT", "TEXT", "MTEXT") and not seg:
            skipped += 1
        for x, y in seg:
            xs.append(x)
            ys.append(y)
    return {"entities": sum(by_type.values()), "by_type": by_type,
            "bbox": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
            "skipped": skipped, "layers": sorted(parsed.get("layers", {}).keys())}


# ------------------------------------------------------------------ 渲染
def render(source: str | dict[str, Any], out_png: str, *, scale: float = 8.0,
           supersample: int = 3, padding: float = 6.0, line_width: float = 1.1,
           colors: dict[str, int] | None = None, background: int = 255,
           extents: Iterable[float] | None = None) -> dict[str, Any]:
    """把 DXF 画成 PNG。

    ``colors`` 可按实体类型或图层名覆盖颜色（值是 ACI 索引），如
    ``{"SPLINE": 7, "WATER": 5}``。返回统计信息，含 ``skipped``。
    """
    from PIL import Image, ImageDraw

    parsed = parse(source) if isinstance(source, str) else source
    stats = summarize(parsed)
    if not stats["entities"]:
        raise DxfError("这份 DXF 里没有实体")
    bbox = list(extents) if extents else stats["bbox"]
    if not bbox:
        raise DxfError("实体都没算出坐标（可能全是文字/点）")
    x0, y0, x1, y1 = bbox[0] - padding, bbox[1] - padding, bbox[2] + padding, bbox[3] + padding
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise DxfError(f"包围盒不合法：{bbox}")

    width, height = max(int((x1 - x0) * scale), 64), max(int((y1 - y0) * scale), 64)
    ss = max(int(supersample), 1)
    if width * height * ss * ss > 400_000_000:
        raise DxfError("图太大，调小 scale / supersample")

    base = ((background, background, background) if isinstance(background, int)
            else tuple(background))
    img = Image.new("RGB", (width * ss, height * ss), base)
    draw = ImageDraw.Draw(img)
    lw = max(1, int(line_width * scale * ss / 2))
    colors = colors or {}
    drawn = 0

    def T(pt):
        return ((pt[0] - x0) * scale * ss, (y1 - pt[1]) * scale * ss)

    for e in parsed["entities"]:
        et = e["type"]
        p = props(e)
        layer = p.get("layer") or "0"
        aci = colors.get(et, colors.get(layer))
        if aci is None:
            aci = p.get("color")
            if aci in (None, 0, 256):
                aci = (parsed["layers"].get(layer) or {}).get("color", 7)
        rgb = aci_rgb(aci)

        if et == "POINT":
            x, y = T(_pt(p, "p"))
            draw.ellipse([x - lw, y - lw, x + lw, y + lw], fill=rgb)
            drawn += 1
            continue
        if et in ("TEXT", "MTEXT") and p.get("text"):
            text = "".join(p["text"])
            size = max(int((p.get("height") or 5.0) * scale * ss * 0.9), 8)
            try:
                from PIL import ImageFont
                font = ImageFont.load_default(size=size)
            except Exception:  # noqa: BLE001
                font = None
            draw.text(T(_pt(p, "p")), text, fill=rgb, font=font)
            drawn += 1
            continue

        seg = polyline_of(e)
        if len(seg) < 2:
            continue
        w = max(int(p["const_width"] * scale * ss / 2), 1) if p.get("const_width") else lw
        draw.line([T(q) for q in seg], fill=rgb, width=w, joint="curve")
        drawn += 1

    img = img.resize((width, height), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    img.save(out_png)
    return {**stats, "drawn": drawn, "png": os.path.abspath(out_png),
            "pixel_size": [width, height], "extents": [x0, y0, x1, y1],
            "bytes": os.path.getsize(out_png)}
