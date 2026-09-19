"""工具层：把 client（无头通道）+ dxf（渲染）拼成给 LLM 用的几个动作。

这一层刻意不依赖 MCP / AstrBot 任何框架——MCP server、AstrBot 插件、
命令行脚本都复用它，避免逻辑散落三处。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Iterable

from . import client as _client
from . import dxf as _dxf

__all__ = ["ToolError", "info", "run_commands", "read_document", "export",
           "preview", "syntax_guard", "resolve_path", "NAMED_COLORS"]

#: 无头 ``run`` 里被静默忽略的命令（实测：返回 completed 但 added=0）
HEADLESS_NOOP = ("TEXT", "MTEXT", "HATCH", "BHATCH", "POLYGON", "-HATCH")

#: 本来就不产生新实体的命令，added=0 是正常的
NON_CREATING = ("ERASE", "ZOOM", "PAN", "UNDO", "REDO", "SELECT", "REGEN", "PURGE",
                "SAVE", "QSAVE", "LAYER", "LAYOFF", "LAYON", "MOVE", "COPY", "MIRROR",
                "ROTATE", "SCALE", "TRIM", "EXTEND", "FILLET", "CHAMFER", "OFFSET", "ARRAY")

NAMED_COLORS = {"black": 7, "red": 1, "yellow": 2, "green": 3, "cyan": 4,
                "blue": 5, "magenta": 6, "gray": 8, "grey": 8,
                "orange": 30, "seagreen": 90, "skyblue": 130, "purple": 190,
                "pink": 210}


class ToolError(RuntimeError):
    """工具层错误：参数不对、路径越界、命令跑不动。"""


# ------------------------------------------------------------------ 路径沙箱
def allowed_roots() -> list[str]:
    raw = os.environ.get("OCADS_ROOTS", "")
    roots = [p for p in raw.split(os.pathsep) if p.strip()]
    if not roots:
        roots = [os.getcwd(), "/tmp", os.path.expanduser("~")]
    return [os.path.abspath(p) for p in roots]


def resolve_path(path: str, *, must_exist: bool = False, suffix: str | None = None) -> str:
    """把用户给的路径规整化并限制在白名单目录内。"""
    if not path or not isinstance(path, str):
        raise ToolError("路径不能为空")
    full = os.path.abspath(os.path.expanduser(path))
    if not any(full == r or full.startswith(r + os.sep) for r in allowed_roots()):
        raise ToolError(f"路径越界：{full} 不在允许目录 {allowed_roots()} 内"
                        f"（用环境变量 OCADS_ROOTS 追加，冒号分隔）")
    if must_exist and not os.path.exists(full):
        raise ToolError(f"文件不存在：{full}")
    if suffix and not full.lower().endswith(suffix):
        raise ToolError(f"只接受 {suffix} 结尾的路径：{full}")
    return full


# ------------------------------------------------------------------ 环境探测
def info() -> dict[str, Any]:
    """报告二进制、版本、无头通道是否可用。"""
    if not os.environ.get("OCADS_ROOTS"):
        return {**_client.discover(), "roots": allowed_roots()}
    return {**_client.discover(), "roots": allowed_roots()}


# ------------------------------------------------------------------ 画图
def syntax_guard(commands: Iterable[str]) -> list[str]:
    """提前拦下无头模式下会静默失败的命令，返回警告列表。"""
    warns = []
    for cmd in commands:
        head = cmd.strip().split(" ", 1)[0].upper()
        if head in HEADLESS_NOOP:
            warns.append(f"{head} 在无头 run 里是静默 no-op（返回 completed 但不会加实体），"
                         f"要文字/填充请走 GUI 或 MCP 的 start-step 流：{cmd!r}")
    return warns


def run_commands(commands: Iterable[str], *, open_path: str | None = None,
                 save_path: str | None = None, timeout: float = 300.0) -> dict[str, Any]:
    """在一个会话里跑一批命令，可选打开/保存。

    返回 ``{"commands": n, "results": [...], "summary": {...}, "saved": path?}``，
    任一命令失败会带上 ``failed`` 列表（不静默吞掉）。
    """
    cmds = [c for c in (commands or []) if str(c).strip()]
    if not cmds and not open_path:
        raise ToolError("既没有命令也没有要打开的文件")
    warnings = syntax_guard(cmds)

    with _client.ServeSession(timeout=timeout) as sess:
        if open_path:
            sess.open(resolve_path(open_path, must_exist=True))
        else:
            sess.new()
        results, failed, no_op = [], [], []
        added = 0
        for cmd in cmds:
            try:
                res = sess.run(cmd)
                got = int(res.get("added") or 0)
                results.append({"cmd": cmd, "ok": True, "added": got})
                added += got
                if got == 0 and cmd.strip().split(" ", 1)[0].upper() not in NON_CREATING:
                    no_op.append(cmd)
            except _client.ServeError as exc:
                results.append({"cmd": cmd, "ok": False, "error": str(exc)})
                failed.append(cmd)
        summary = sess.entities()
        out: dict[str, Any] = {"commands": len(cmds), "added": added, "failed": failed,
                               "no_op": no_op, "summary": summary.get("by_type") or summary,
                               "warnings": warnings, "results": results}
        if save_path:
            target = resolve_path(save_path)
            sess.save(target)
            out["saved"] = target
        return out


# ------------------------------------------------------------------ 读 / 验真
def read_document(op: str = "entities", *, open_path: str | None = None,
                  parameters: dict[str, Any] | None = None,
                  timeout: float = 120.0) -> dict[str, Any]:
    """读图纸：entities / records / query / intersections / near / layers / header / capabilities。

    注意：面积、长度这类 ``measure`` 只在 GUI MCP 里有，无头 ``--serve`` 不提供。
    无头能做内核验真的是 ``intersections``（真交点+参数）和 ``near``（最近实体+距离）。
    """
    op = (op or "entities").strip()
    params = dict(parameters or {})
    known = ("entities", "layers", "header", "capabilities", "records", "query",
             "intersections", "near")
    if op == "measure":
        raise ToolError("无头 --serve 没有 measure（面积/长度）这个 op，那是 GUI MCP 专属；"
                        "改用 intersections / near / query(detail='full') 做内核验真")
    if op not in known:
        raise ToolError(f"不支持的 op：{op}（可用 {'/'.join(known)}）")
    with _client.ServeSession(timeout=timeout) as sess:
        if open_path:
            sess.open(resolve_path(open_path, must_exist=True))
        else:
            sess.new()
        if op in ("entities", "layers", "header", "capabilities"):
            return sess.request(op)
        if op == "records":
            return sess.records(**params)
        if op == "query":
            return sess.query(**params)
        if op == "intersections":
            handles = params.get("handles") or []
            if len(handles) != 2:
                raise ToolError("intersections 需要 parameters.handles 正好两个句柄")
            return sess.intersections(str(handles[0]), str(handles[1]))
        if op == "near":
            point = params.get("point") or params.get("near")
            if not point or len(point) < 2:
                raise ToolError("near 需要 parameters.point = [x, y]")
            extra = {k: v for k, v in params.items() if k not in ("point", "near")}
            return sess.near(float(point[0]), float(point[1]), **extra)
        raise ToolError(f"不支持的 op：{op}")


# ------------------------------------------------------------------ 导出
def export(src: str, dst: str, *, timeout: float = 180.0) -> dict[str, Any]:
    """一次性无头转换。注意：只支持 .dwg / .dxf 目标（PDF/PNG 上游还没开放）。"""
    src_full = resolve_path(src, must_exist=True)
    dst_full = resolve_path(dst)
    if not dst_full.lower().endswith((".dwg", ".dxf")):
        raise ToolError("--export 只支持 .dwg/.dxf 目标；要图请用 preview 工具")
    binary = _client.find_binary()
    proc = subprocess.run([binary, "--export", src_full, dst_full],
                          capture_output=True, text=True, timeout=timeout)
    ok = proc.returncode == 0 and os.path.isfile(dst_full)
    return {"ok": ok, "exit_code": proc.returncode, "output": (proc.stdout or proc.stderr).strip()[:400],
            "dst": dst_full, "bytes": os.path.getsize(dst_full) if os.path.isfile(dst_full) else 0}


# ------------------------------------------------------------------ 预览（看图）
def preview(dxf_path: str, png_path: str | None = None, *, scale: float = 8.0,
            line_width: float = 1.1, supersample: int = 3,
            colors: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 DXF 渲染成 PNG（渲染的是文件本身，可当验真手段）。"""
    src = resolve_path(dxf_path, must_exist=True)
    dst = resolve_path(png_path or (os.path.splitext(src)[0] + "_preview.png"))
    palette: dict[str, int] = {}
    for key, value in (colors or {}).items():
        if isinstance(value, str):
            named = NAMED_COLORS.get(value.strip().lower())
            if named is None:
                try:
                    palette[key] = int(value)
                except ValueError:
                    raise ToolError(f"颜色 {value!r} 既不是名字也不是索引；可用："
                                    f"{sorted(NAMED_COLORS)}") from None
            else:
                palette[key] = named
        else:
            palette[key] = int(value)
    return _dxf.render(src, dst, scale=scale, line_width=line_width,
                       supersample=supersample, colors=palette or None)


def tool_schemas() -> list[dict[str, Any]]:
    """MCP 工具清单（与 mcp_server.py 共用，保证 schema 只写一处）。"""
    roots = os.pathsep.join(allowed_roots())
    return [
        {
            "name": "ocads_info",
            "description": "探测 OpenCADStudio 二进制、版本、无头通道（--serve）是否可用，以及允许写入的目录。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "ocads_run",
            "description": ("在一个 OpenCADStudio 无头会话里按顺序执行 CAD 命令（每行必须是**完整**命令，"
                            "含所有提示答案），可选打开已有图纸、保存为新图纸，并返回实体统计。"
                            f"允许写入目录：{roots}"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "commands": {"type": "array", "items": {"type": "string"},
                                 "description": "完整命令行，如 'LINE 0,0 10,0'、'PLINE 0,0 10,0 10,10 C'、'ELLIPSE 0,0 40,0 20'、'ARC 0,0 5,5 10,0'、'SPLINE 0,0 20,10 40,0 C'、'DONUT 0 9 0,0'(实心盘)、'CIRCLE 0,0 10'、'RECTANG -5,-5 5,5'"},
                    "open_path": {"type": "string", "description": "可选：先打开这个 dxf/dwg（不给则新建）"},
                    "save_path": {"type": "string", "description": "可选：结束前保存到该路径（.dxf/.dwg）"},
                },
                "required": ["commands"],
            },
        },
        {
            "name": "ocads_read",
            "description": ("读图纸 / 验真：entities（实体清单）、records（完整记录+属性）、query（type/layer/handles/near/"
                            "contains_point/bounds/detail）、intersections（两个句柄的内核真交点）、near（最近实体+距离）、"
                            "layers、header、capabilities。注意：面积/长度类 measure 只在 GUI MCP 里有，无头不提供。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["entities", "records", "query", "intersections",
                                                      "near", "layers", "header", "capabilities"]},
                    "open_path": {"type": "string", "description": "可选：读这份文件（不给则读空图）"},
                    "parameters": {"type": "object", "description": "透传参数：records 用 {\"collection\":\"entities\"}；query 用 {\"type\":\"Circle\",\"detail\":\"full\"}；intersections 用 {\"handles\":[\"63\",\"64\"]}；near 用 {\"point\":[0,0]}"},
                },
                "required": ["op"],
            },
        },
        {
            "name": "ocads_export",
            "description": "一次性无头格式转换（--export）。只支持 .dwg/.dxf；要 PNG 请用 ocads_preview。",
            "inputSchema": {
                "type": "object",
                "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                "required": ["src", "dst"],
            },
        },
        {
            "name": "ocads_preview",
            "description": "把 DXF 渲染成 PNG（自带渲染器，读文件本身，可当验收手段）。colors 可按实体类型或图层名指定颜色，如 {\"SPLINE\":\"red\",\"WATER\":\"blue\"}。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dxf_path": {"type": "string"},
                    "png_path": {"type": "string"},
                    "scale": {"type": "number", "description": "每单位多少像素，默认 8"},
                    "line_width": {"type": "number", "description": "线宽（单位），默认 1.1"},
                    "colors": {"type": "object", "description": "类型/图层 → ACI 索引或颜色名"},
                },
                "required": ["dxf_path"],
            },
        },
    ]


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """按名字分发（MCP server 和 AstrBot 插件共用入口）。"""
    args = arguments or {}
    try:
        if name == "ocads_info":
            return info()
        if name == "ocads_run":
            return run_commands(args.get("commands") or [], open_path=args.get("open_path"),
                                save_path=args.get("save_path"))
        if name == "ocads_read":
            return read_document(args.get("op", "entities"), open_path=args.get("open_path"),
                                 parameters=args.get("parameters"))
        if name == "ocads_export":
            return export(args["src"], args["dst"])
        if name == "ocads_preview":
            return preview(args["dxf_path"], args.get("png_path"), scale=float(args.get("scale", 8.0)),
                           line_width=float(args.get("line_width", 1.1)),
                           colors=args.get("colors"))
    except (ToolError, _client.ServeError, _dxf.DxfError, KeyError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "error": f"未知工具：{name}"}


if __name__ == "__main__":            # 命令行手动试跑：python -m ocads.tools info
    import sys
    print(json.dumps(call_tool(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}),
                     ensure_ascii=False, indent=1))
