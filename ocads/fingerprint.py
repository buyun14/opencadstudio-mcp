"""截图去重指纹：``if_changed`` 的底座。

为什么要它：每张截图都要进模型上下文（一张 1600px 视口 ≈ 一两千 token），而改图是个
"改→截图→看→再改"的循环，**很多轮之间画面根本没变**，重复塞同一张图纯属烧钱。
带了 `if_changed` 之后，没变就只回一小段 ``{"changed": false}``，模型用几十个 token
就知道"画面没动"。

实现要点（这行有个反直觉的坑）：**逐格数像素，不要算平均**。
1 像素宽的线画在 1600px 图上，平均下来几乎为 0，用平均值会把"多了一根线"误判成没变化。
所以先把图转灰度阈值化成"墨迹掩码"（0/1），再用 BOX 缩到 G×G —— BOX 平均 0/1 掩码
得到的就是**每格里墨迹像素的占比**，既不丢细线，又是 C 级速度。
"""

from __future__ import annotations

import json
import os
from typing import Any

__all__ = ["FingerprintError", "signature", "diff_ratio", "check", "sidecar_path"]

DEFAULT_GRID = 12
DEFAULT_THRESHOLD = 0.005      # 允许 0.5% 的格子变化；低于此判定"没变"
                               # 注意：格子要先被 TILE_EPS 判"变了"才计入这个比例，
                               # 所以 threshold=0 也不等于"任何像素差异都算变"
TILE_EPS = 0.01                # 单格墨迹占比变化超过 1% 才算这格变了
INK_LEVEL = 200                # 灰度低于它就当墨迹（背景默认白）


class FingerprintError(RuntimeError):
    """指纹算不了（图读不出来、格式不支持）。"""


def sidecar_path(png_path: str) -> str:
    """指纹存在图旁边的 ``<png>.sig.json``，跟着图一起清理。"""
    return png_path + ".sig.json"


def _flatten(img):
    """透明/调色板图先铺到白底再算墨迹。

    直接 ``convert("L")`` 会把 alpha 丢掉，而透明区往往读成黑——会被误当墨迹。
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = __import__("PIL.Image", fromlist=["Image"]).new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return img


def signature(png_path: str, grid: int = DEFAULT_GRID) -> dict[str, Any]:
    """算一张图的墨迹指纹：``{"grid": n, "tiles": [...], "ink": 占比}``。"""
    from PIL import Image

    if not os.path.isfile(png_path):
        raise FingerprintError(f"图不存在：{png_path}")
    try:
        with Image.open(png_path) as img:
            size = [img.width, img.height]
            flat = _flatten(img)
            mask = flat.convert("L").point(lambda v: 255 if v < INK_LEVEL else 0)
            # BOX 缩放在 0/255 掩码上等价于"该格里墨迹像素的比例"
            small = mask.resize((grid, grid), Image.BOX)
            # tobytes() 而不是 getdata()：后者在 Pillow 14 会被移除
            tiles = [round(v / 255.0, 4) for v in small.tobytes()]
    except Exception as exc:  # noqa: BLE001
        raise FingerprintError(f"读不出图 {png_path}：{exc}") from exc
    return {"grid": grid, "tiles": tiles, "ink": round(sum(tiles) / len(tiles), 4),
            "size": size}


def diff_ratio(prev: dict[str, Any], cur: dict[str, Any]) -> float:
    """两张指纹里"变了"的格子占比（0.0~1.0）。格数或尺寸不一致直接算全变。"""
    if not prev or prev.get("grid") != cur.get("grid") or prev.get("size") != cur.get("size"):
        return 1.0
    a, b = prev.get("tiles") or [], cur.get("tiles") or []
    if len(a) != len(b) or not a:
        return 1.0
    changed = sum(1 for x, y in zip(a, b) if abs(x - y) > TILE_EPS)
    return changed / len(a)


def check(png_path: str, *, threshold: float = DEFAULT_THRESHOLD,
          grid: int = DEFAULT_GRID, update: bool = True,
          source: str | None = None) -> dict[str, Any]:
    """和上次的指纹比一比。

    ``source`` 是"这张图代表什么"的身份串（比如 文档 id + 命令 + 视图）。不带它的话
    指纹只按文件路径认——同一个路径换了另一份内容但长得像，就会被误判成"没变"。
    带上它，身份一变即视为基线（稳妥优先）。

    基线策略：只在 **首次** 或 **判定有变化** 时更新指纹文件。否则一串"每次差一点点、
    都低于阈值"的微改会让基线越漂越远，最后和调用方真正看过的画面脱节。
    """
    cur = signature(png_path, grid=grid)
    path = sidecar_path(png_path)
    prev = None
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            prev = None
    if prev is not None and prev.get("source") != source:
        prev = None            # 来源对不上（含"这次没给 source、上次给了"）就不能拿来比
    baseline = prev is None
    ratio = 1.0 if baseline else diff_ratio(prev, cur)
    changed = baseline or ratio > threshold

    warning = None
    if update and (baseline or changed):
        cur["source"] = source
        try:
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh)
            os.replace(tmp, path)                     # 原子替换，别让并发读到半个 JSON
        except OSError as exc:                        # 记账失败不该把截图结果一起废掉
            warning = f"指纹没写进去（不影响图）：{exc}"
    out = {"changed": changed, "ratio": round(ratio, 4), "baseline": baseline,
           "previous_ink": (prev or {}).get("ink", cur["ink"]), "ink": cur["ink"],
           "threshold": threshold, "tile_eps": TILE_EPS, "source": source}
    if warning:
        out["fingerprint_warning"] = warning
    return out
