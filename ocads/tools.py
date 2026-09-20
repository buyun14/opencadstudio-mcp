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
from . import fingerprint as _fp
from . import mcp_client as _mcp

__all__ = ["ToolError", "info", "run_commands", "read_document", "export",
           "preview", "capture", "set_properties", "set_view", "syntax_guard",
           "resolve_path", "NAMED_COLORS", "ENGINES"]

#: 两条通道：``serve`` = 无头快通道（无窗口、快、缺量测/截图/交互命令）
#: ``mcp`` = 上游 GUI MCP（Xvfb 下可用，有量测、原厂渲染、交互步骤）
ENGINES = ("serve", "mcp")

#: 批处理 ``run`` 里仍然会静默空转的命令（2026.38.0 实测：返回 completed 但 added=0）。
#: ``TEXT`` 曾在这份名单里，上游已修（旧格式 ``run`` 现在会把文字写进画布编辑器并提交）。
HEADLESS_NOOP = ("MTEXT", "HATCH", "BHATCH", "POLYGON", "-HATCH")

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
            warns.append(f"{head} 在批处理 run 里仍是静默空转（返回 completed 但不会加实体），"
                         f"它需要交互式界面（图案选择/边界选择）：{cmd!r}")
    return warns


def run_commands(commands: Iterable[str], *, open_path: str | None = None,
                 save_path: str | None = None, engine: str = "serve",
                 capture_png: str | None = None, timeout: float = 300.0) -> dict[str, Any]:
    """在一个会话里跑一批命令，可选打开/保存/顺带截图。

    ``engine="serve"``：无头快通道；``engine="mcp"``：上游 GUI MCP（能截图、能量测、
    能用需要交互步骤的命令，但慢一些，且要 xvfb 或 DISPLAY）。

    返回 ``{"commands","added","failed","no_op","summary","results",…}``，
    失败与空转命令都会点名，不静默吞掉。
    """
    cmds = [c for c in (commands or []) if str(c).strip()]
    if not cmds and not open_path:
        raise ToolError("既没有命令也没有要打开的文件")
    engine = (engine or "serve").lower()
    if engine not in ENGINES:
        raise ToolError(f"engine 只能是 {'/'.join(ENGINES)}")
    warnings = syntax_guard(cmds)
    if engine == "mcp":
        return _run_via_mcp(cmds, open_path=open_path, save_path=save_path,
                            capture_png=capture_png, warnings=warnings, timeout=timeout)

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
                  parameters: dict[str, Any] | None = None, engine: str = "serve",
                  timeout: float = 120.0) -> dict[str, Any]:
    """读图纸。

    ``serve`` 引擎（默认，无需窗口）有两条通道：

    * **旧格式**（``{"op": ...}``）：``entities`` / ``records`` / ``query`` /
      ``intersections`` / ``near`` / ``layers`` / ``header`` / ``capabilities``
    * **协议 1**（``{"protocol":1, ...}``，走的和控制面同一套）：``measure``（内核算出的
      长度/面积/包围盒/质量特性）、``commands``（命令清单，含批处理写法）、``properties``、
      ``history``、``state``

    ``capture`` 两者都没有：无窗口时它回 ``gui_required``，要截图请用 ``ocads_capture``
    （mcp 引擎）。
    """
    op = (op or "entities").strip()
    params = dict(parameters or {})
    engine = (engine or "serve").lower()
    if engine not in ENGINES:
        raise ToolError(f"engine 只能是 {'/'.join(ENGINES)}")
    legacy = ("entities", "records", "query", "intersections", "near", "layers",
              "header", "capabilities")
    protocol_one = ("measure", "commands", "properties", "history", "state")
    if op not in legacy + protocol_one:
        raise ToolError(f"不支持的 op：{op}（可用 {'/'.join(legacy + protocol_one)}）")

    if engine == "mcp":
        with _mcp.UpstreamMCP(timeout=timeout) as m:
            prep = m.prepare_document(document=params.get("document"))
            sid, did = prep["session"], prep["document"]
            if open_path:
                m.call("open", session=sid, document=did,
                       path=resolve_path(open_path, must_exist=True))
            if op == "measure":
                handles = params.get("handles") or m.handles(did, session=sid)
                handles = list(handles)[: int(params.get("limit") or 20)]
                if not handles:
                    raise ToolError("这份图纸里没有可量测的实体")
                return m.measure(handles, did, session=sid)
            if op == "commands":
                args: dict[str, Any] = {"ocs_session_id": sid}
                if params.get("name"):
                    args["parameters"] = {"name": params["name"]}
                return m.tool("ocs_read", args)
            if op in ("records", "query"):
                return m.read(op, session=sid, document=did,
                              parameters=params or {"collection": "entities"})
            return m.read(op, session=sid, document=did)

    with _client.ServeSession(timeout=timeout) as sess:
        if open_path:
            sess.open(resolve_path(open_path, must_exist=True))
        did = params.get("document")
        if op == "commands":
            return sess.command_manifest(params.get("name"))
        if op in ("properties", "history", "state"):
            return sess.control_must(op, document_id=did)
        if op == "measure":                      # 协议 1：无头也能量
            if did is None:
                did = sess.control("state").get("document_id")
            handles = params.get("handles")
            if not handles:
                recs = sess.control_must("records", document_id=did, collection="entities")
                handles = [r["handle"] for r in recs.get("records", []) if r.get("handle")]
            handles = list(handles)[: int(params.get("limit") or 20)]
            if not handles:
                raise ToolError("这份图纸里没有可量测的实体（也可以先选中再让 measure 用选区）")
            return sess.measure(handles, did)
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
        if op in ("records", "query"):
            return sess.request(op, **params)
        return sess.request(op)


def _run_via_mcp(cmds: list[str], *, open_path: str | None, save_path: str | None,
                capture_png: str | None, warnings: list[str], timeout: float) -> dict[str, Any]:
    """走上游 GUI MCP 跑命令：清弹窗 → 画 → 回读句柄 → 顺带内核量测/截图/存盘。"""
    with _mcp.UpstreamMCP(timeout=timeout) as m:
        prep = m.prepare_document()
        sid, did = prep["session"], prep["document"]
        if open_path:
            m.call("open", session=sid, document=did,
                   path=resolve_path(open_path, must_exist=True))
        res = m.run_many(cmds, did, session=sid)
        handles = m.handles(did, session=sid)
        out: dict[str, Any] = {"engine": "mcp", "commands": len(cmds), "added": res["ok"],
                               "failed": res["failed"], "no_op": res["no_op"],
                               "waiting_input": res.get("waiting_input"),
                               "warnings": warnings,
                               "modals_closed": prep["modals"]["closed"],
                               "modal_left": prep["modals"]["left"],
                               "entities": len(handles), "handles": handles, "document": did}
        if handles:
            try:
                out["measurements"] = m.measure(handles[:20], did, session=sid)
            except _mcp.MCPError as exc:
                out["measure_error"] = str(exc)
        if capture_png:
            out["capture"] = m.capture(resolve_path(capture_png), did, session=sid)
        if save_path:
            target = resolve_path(save_path)
            m.save(target, did, session=sid)
            out["saved"] = target
        return out


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
            colors: dict[str, Any] | None = None, if_changed: bool = False,
            threshold: float = 0.005) -> dict[str, Any]:
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
    out = _dxf.render(src, dst, scale=scale, line_width=line_width,
                      supersample=supersample, colors=palette or None)
    try:
        stamp = os.path.getmtime(src)
    except OSError:
        stamp = 0
    source = f"render:{src}:{stamp}:{scale}:{line_width}:{sorted(palette.items())}"
    out.update(_fingerprint(dst, if_changed=if_changed, threshold=threshold, source=source))
    return out


# ------------------------------------------------------------------ 原厂渲染截图
def capture(png_path: str, *, commands: Iterable[str] | None = None, dxf_path: str | None = None,
            target: str = "viewport", max_size: int = 1600, zoom: bool = True,
            view: str | None = None, if_changed: bool = False, threshold: float = 0.005,
            timeout: float = 300.0) -> dict[str, Any]:
    """用 **OpenCADStudio 自己的渲染器**出图（区别于 ``preview`` 的自绘渲染器）。

    在一个 GUI MCP 会话里：清弹窗 → 新建（或打开 ``dxf_path``）→ 跑 ``commands`` → 截图。
    需要 xvfb 或 DISPLAY；软件渲染（llvmpipe）下画面里可能叠着 GPU 警告弹窗。
    """
    dst = resolve_path(png_path)
    cmds = [c for c in (commands or []) if str(c).strip()]
    with _mcp.UpstreamMCP(timeout=timeout) as m:
        prep = m.prepare_document()
        sid, did = prep["session"], prep["document"]
        if dxf_path:
            m.call("open", session=sid, document=did,
                   path=resolve_path(dxf_path, must_exist=True))
        ran = m.run_many(cmds, did, session=sid) if cmds else {"ok": 0, "failed": [], "no_op": []}
        if view:
            m.set_view(view, did, session=sid)      # 画完再定视图，取景才对得上
        shot = m.capture(dst, did, session=sid, target=target, max_size=max_size,
                         zoom=zoom and view not in ("home", "extents"))
        out = {**shot, "engine": "mcp", "document": did,
               "entities": len(m.handles(did, session=sid)),
               "failed": ran["failed"], "no_op": ran["no_op"],
               "modals_closed": prep["modals"]["closed"], "modal_left": prep["modals"]["left"]}
        source = f"mcp:{did}:{target}:{max_size}:{view or ''}:{'|'.join(cmds)[:200]}"
        out.update(_fingerprint(dst, if_changed=if_changed, threshold=threshold, source=source))
        return out


# ------------------------------------------------------------------ 真改属性
def _fingerprint(dst: str, *, if_changed: bool, threshold: float,
                 source: str | None) -> dict[str, Any]:
    """算/比指纹，永不抛错——指纹只是省钱手段，不能把截图本身搞失败。"""
    if not if_changed:
        return {}
    try:
        res = _fp.check(dst, threshold=threshold, source=source)
    except _fp.FingerprintError as exc:
        return {"fingerprint_error": str(exc)}
    except ImportError as exc:                     # 没装 Pillow
        return {"fingerprint_error": f"缺 Pillow，跳过去重：{exc}"}
    if not res["changed"]:
        res["reuse_previous"] = dst                 # 内容与上次实质相同，不必重复看图
        res["note"] = "画面与上次实质相同；图已覆盖为当前帧，无需再看"
    return res


def _normalize_update(update: dict[str, Any]) -> dict[str, Any]:
    """把好写好记的值转成上游要的序列化枚举。

    上游的 ``/common/color`` 要 ``{"Index": 1}`` 或 ``"ByLayer"``，
    直接给 ``"Red"``/``1`` 会回 ``invalid_value``（实测）。
    """
    if not isinstance(update, dict) or "path" not in update:
        raise ToolError(f"updates 里每项都要有 path：{update!r}")
    out = dict(update)
    path = str(out["path"])
    value = out.get("value")
    if path.endswith("/color"):
        if isinstance(value, bool):
            raise ToolError("颜色别给布尔值")
        if isinstance(value, (int, float)):
            out["value"] = {"Index": int(value)}
        elif isinstance(value, str):
            named = NAMED_COLORS.get(value.strip().lower())
            if named is not None:
                out["value"] = {"Index": named}
            elif value in ("ByLayer", "ByBlock"):
                out["value"] = value
            else:
                raise ToolError(f"颜色 {value!r} 认不出来；可用颜色名（{'/'.join(sorted(NAMED_COLORS))}）"
                                f"、ACI 索引、或 'ByLayer'/'ByBlock'")
    return out


def set_properties(handle: str, updates: list[dict[str, Any]], *, collection: str = "entities",
                   document: Any = None, timeout: float = 120.0) -> dict[str, Any]:
    """改记录属性（实体颜色/图层/线型…）——只有 GUI MCP 引擎能做，改的是图纸数据本身。

    ``updates`` 形如 ``[{"path": "/common/color", "value": "red"}]``（颜色名 / ACI 索引 /
    ``ByLayer`` 都可以，本函数会转成上游要的序列化枚举）。
    具体路径与取值域用 ``ocads_read(op="records")`` 读出来对照，或在上游
    ``record_schema`` 里查（serve 引擎没有这个 op）。
    """
    if not handle or not updates:
        raise ToolError("set_properties 需要 handle 和 updates")
    normalized = [_normalize_update(u) for u in updates]
    with _mcp.UpstreamMCP(timeout=timeout) as m:
        prep = m.prepare_document(document=document)
        return m.set_properties(handle, normalized, prep["document"], session=prep["session"],
                                collection=collection)


def set_view(name: str, *, document: Any = None, timeout: float = 120.0) -> dict[str, Any]:
    """切换视图。``name`` 只支持上游给的两个：

    - ``home``：回到 Home 视图（``action: view_home``，实测回 completed，干净）
    - ``extents``：缩放到图形范围（``action: zoom_extents``，会回 waiting_input 但已生效，
      内部会补一发 Esc 收尾）

    想要顶视/前视/等轴测这类**标准面视图**，上游没有对应 action，只能像 ocs-webmcp 那样
    点 ViewCube 的像素坐标（``pointer_press``/``pointer_release``）—— 坐标跟窗口尺寸绑定，
    比较脆，这里先不提供。
    """
    if name not in ("home", "extents"):
        raise ToolError(f"view 只能是 home/extents（要标准面视图得点 ViewCube，见文档）：{name!r}")
    with _mcp.UpstreamMCP(timeout=timeout) as m:
        prep = m.prepare_document(document=document)
        sid, did = prep["session"], prep["document"]
        res = (m.set_view(name, did, session=sid))
        return {"ok": res.get("status") in ("completed", "ok", "waiting_input"),
                "status": res.get("status"), "document": did}


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
                    "engine": {"type": "string", "enum": ["serve", "mcp"],
                               "description": "serve=无头快通道（默认）；mcp=上游 GUI MCP，慢但支持需要交互步骤的命令"},
                    "capture_png": {"type": "string", "description": "可选：跑完用原厂渲染器截图到该 PNG（隐含 engine=mcp）"},
                },
                "required": ["commands"],
            },
        },
        {
            "name": "ocads_read",
            "description": ("读图纸 / 验真。serve 引擎：entities、records、query（type/layer/handles/near/"
                            "contains_point/bounds/detail）、intersections（两实体内核真交点）、near（最近实体+距离）、"
                            "layers、header、capabilities；协议 1 另加 measure（内核量测：长度/面积/包围盒/质量特性）、"
                            "commands（命令清单+批处理写法）、properties、history、state。"
                            "capture 在无窗口下回 gui_required，截图请用 ocads_capture。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["entities", "records", "query", "intersections",
                                                      "near", "layers", "header", "capabilities",
                                                      "measure", "commands", "properties", "history", "state"]},
                    "open_path": {"type": "string", "description": "可选：读这份文件（不给则读空图）"},
                    "parameters": {"type": "object", "description": "透传参数：records→{\"collection\":\"entities\"}；query→{\"type\":\"Circle\",\"detail\":\"full\"}；intersections→{\"handles\":[\"63\",\"64\"]}；near→{\"point\":[0,0]}；measure→{\"handles\":[\"63\"]}"},
                    "engine": {"type": "string", "enum": ["serve", "mcp"],
                               "description": "两条都能量测；capture 只有 mcp 可行"},
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
            "name": "ocads_capture",
            "description": ("用 OpenCADStudio **自己的渲染器**出图（不是自绘），需要 xvfb 或 DISPLAY。"
                            "一个会话里：清启动弹窗 → 新建或打开 dxf_path → 跑 commands → 截图。"
                            "软件渲染时画面里可能叠着 GPU 警告弹窗。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "png_path": {"type": "string", "description": "输出 PNG 路径"},
                    "commands": {"type": "array", "items": {"type": "string"}, "description": "可选：先画的完整命令"},
                    "dxf_path": {"type": "string", "description": "可选：先打开的 dxf/dwg（与 commands 二选一或都用）"},
                    "target": {"type": "string", "enum": ["viewport", "window"], "description": "截视口还是整个窗口"},
                    "max_size": {"type": "number", "description": "长边像素上限，默认 1600"},
                    "zoom": {"type": "boolean", "description": "截图前缩放到图形范围，默认 true；指定 view=home/extents 时该项自动让位给 view"},
                    "view": {"type": "string", "enum": ["home", "extents"], "description": "可选：先切视图（home=Home 视图，extents=缩放到范围）"},
                    "if_changed": {"type": "boolean", "description": "true 时和上次截图比指纹，没变则返回 changed=false 且复用旧图（省 token）"},
                    "threshold": {"type": "number", "description": "允许变化的格子占比，默认 0.005"},
                },
                "required": ["png_path"],
            },
        },
        {
            "name": "ocads_set_properties",
            "description": "改记录属性（实体线上色/换图层），只有 GUI MCP 能做，改的是图纸数据本身。updates 形如 [{\"path\":\"/common/color\",\"value\":\"red\"}]，颜色名/ACI 索引/ByLayer 都会自动转成上游要的形式。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "实体句柄，可从 ocads_read(op='entities') 或 run 返回的 handles 拿"},
                    "updates": {"type": "array", "items": {"type": "object"}, "description": "[{\"path\":\"/common/color\",\"value\":\"red\"}]"},
                    "collection": {"type": "string", "description": "records 集合，默认 entities"},
                    "document": {"description": "可选：目标 document_id"},
                },
                "required": ["handle", "updates"],
            },
        },
        {
            "name": "ocads_set_view",
            "description": ("切视图：home（Home 视图）/ extents（缩放到图形范围）。上游只有这两个视图动作，"
                            "没有顶视/前视/等轴测这类标准面视图（那要点 ViewCube，坐标跟窗口尺寸绑定）。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["home", "extents"]},
                    "document": {"description": "可选：目标 document_id"},
                },
                "required": ["name"],
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
                    "if_changed": {"type": "boolean", "description": "true 时比指纹，没变则 changed=false + reuse_previous"},
                    "threshold": {"type": "number", "description": "允许变化的格子占比，默认 0.005"},
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
                                save_path=args.get("save_path"),
                                engine=args.get("engine") or ("mcp" if args.get("capture_png") else "serve"),
                                capture_png=args.get("capture_png"))
        if name == "ocads_read":
            return read_document(args.get("op", "entities"), open_path=args.get("open_path"),
                                 parameters=args.get("parameters"),
                                 engine=args.get("engine") or "serve")
        if name == "ocads_capture":
            return capture(args["png_path"], commands=args.get("commands"),
                           dxf_path=args.get("dxf_path"), target=args.get("target", "viewport"),
                           max_size=int(args.get("max_size", 1600)),
                           zoom=bool(args.get("zoom", True)), view=args.get("view"),
                           if_changed=bool(args.get("if_changed", False)),
                           threshold=float(args.get("threshold", 0.005)))
        if name == "ocads_set_view":
            return set_view(args["name"], document=args.get("document"))
        if name == "ocads_set_properties":
            return set_properties(args["handle"], args["updates"],
                                  collection=args.get("collection", "entities"),
                                  document=args.get("document"))
        if name == "ocads_export":
            return export(args["src"], args["dst"])
        if name == "ocads_preview":
            return preview(args["dxf_path"], args.get("png_path"), scale=float(args.get("scale", 8.0)),
                           line_width=float(args.get("line_width", 1.1)),
                           colors=args.get("colors"),
                           if_changed=bool(args.get("if_changed", False)),
                           threshold=float(args.get("threshold", 0.005)))
    except (ToolError, _client.ServeError, _dxf.DxfError, _fp.FingerprintError,
            ImportError, KeyError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "error": f"未知工具：{name}"}


if __name__ == "__main__":            # 命令行手动试跑：python -m ocads.tools info
    import sys
    print(json.dumps(call_tool(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}),
                     ensure_ascii=False, indent=1))
