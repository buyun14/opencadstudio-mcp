"""上游 GUI MCP 通道（``OpenCADStudio --mcp``）的封装。

为什么要有这一层：``--serve`` 是无头快通道，但**缺**三样东西——面积/长度量测、
原厂渲染截图、以及 TEXT/HATCH 这类需要交互步骤的命令。上游自带的 MCP 全都有，
只要解决两个坑：

1. **模态框**：启动后 `AssocPrompt`、然后 `DonationPrompt` 会挡住 `new`，导致
   ``{"op":"new"}`` 返回 ok 却建不出图纸（上游 issue #1349 描述过同一个现象）。
   用 ``{"op":"action","name":"close_modal"}`` 关掉即可——比发 ``cancel``
   （等价按 Esc）精准，不会误伤别的交互。只关白名单里的弹窗，其它弹窗如实上报。
2. **document_id**：读和写都盯着"活动标签页"，新建完活动页可能还停在开始页，
   所以后续调用必须显式带 ``document_id``。

无显示的机器上靠 ``xvfb-run`` 起一个虚拟屏（软件渲染即可，实测能用）。
"""

from __future__ import annotations

import base64
import json
import os
import pwd
import shutil
import signal
import subprocess
import time
from typing import Any, Iterable

from .client import ServeError, find_binary

__all__ = ["MODAL_ALLOWLIST", "UpstreamMCP", "MCPError"]

#: 允许自动关闭的弹窗。别的弹窗一律不碰，只上报——照抄 ocs-webmcp 的安全姿态，
#: 只是无头环境下多放行几个"纯提示"性质的。
MODAL_ALLOWLIST = {"AssocPrompt", "DonationPrompt", "UpdateNotice", "About"}

_PROTOCOL = "2026-07-28"
_XVFB_ARGS = ["-a", "--server-args=-screen 0 1600x1200x24"]


class MCPError(ServeError):
    """GUI MCP 通道出错。"""


class UpstreamMCP:
    """一条 ``--mcp`` 会话（含它拉起来的编辑器实例）。

    用法::

        with UpstreamMCP() as m:
            sid, did = m.prepare_document()      # 清弹窗 + 新建并激活图纸
            m.run("CIRCLE 0,0 10", did)
            print(m.measure([h for h in m.handles(did)][:1], did))
            print(m.capture("/tmp/view.png", did))
    """

    def __init__(self, binary: str | None = None, *, use_xvfb: bool | None = None,
                 timeout: float = 120.0, cwd: str | None = None):
        self.binary = binary or find_binary()
        self.timeout = timeout
        self.closed_modals: list[str] = []
        env = dict(os.environ)
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        # 上游要一个"用户配置目录"，缺了就回 No user configuration directory；
        # 服务环境里 HOME 常常是空的，这里兜底并保证目录存在。
        home = env.get("HOME")
        if not home or not os.path.isdir(home):
            home = pwd.getpwuid(os.getuid()).pw_dir
        env["HOME"] = home
        config_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        os.makedirs(config_home, exist_ok=True)
        env["XDG_CONFIG_HOME"] = config_home
        if use_xvfb is None:
            use_xvfb = not env.get("DISPLAY")
        if use_xvfb and not shutil.which("xvfb-run"):
            raise MCPError("没有 DISPLAY 也没装 xvfb-run，GUI MCP 通道起不来；"
                           "装一个 xvfb，或者改用 engine='serve'")
        cmd = [self.binary, "--mcp"]
        if use_xvfb:
            cmd = ["xvfb-run", *_XVFB_ARGS, *cmd]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=cwd, text=True, bufsize=1, env=env,
                                     start_new_session=True)
        self._serial = 0
        self._initialize()

    # ---------------------------------------------------------------- 协议
    def _rpc(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._serial += 1
        payload = {"jsonrpc": "2.0", "id": self._serial, "method": method, "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")          # type: ignore[union-attr]
            self.proc.stdin.flush()                                    # type: ignore[union-attr]
        except (BrokenPipeError, ValueError) as exc:
            raise MCPError(f"通道断了（{method}）：{self.drain_stderr()}") from exc
        deadline = time.time() + (timeout or self.timeout)
        while True:
            if time.time() > deadline:
                raise MCPError(f"{method} 超时（{timeout or self.timeout}s）")
            line = self.proc.stdout.readline()                        # type: ignore[union-attr]
            if not line:
                raise MCPError(f"{method} 没响应，进程退了：{self.drain_stderr()}")
            msg = json.loads(line)
            if msg.get("id") != self._serial:
                continue                                               # 通知/旧响应，跳过
            if "error" in msg:
                raise MCPError(f"{method} 失败：{json.dumps(msg['error'], ensure_ascii=False)[:300]}")
            return msg["result"]

    def _initialize(self) -> None:
        self._rpc("initialize", {"protocolVersion": _PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "ocads", "version": "0.2.0"}}, timeout=90)

    def tool(self, name: str, args: dict[str, Any]) -> Any:
        """调一个 MCP 工具，返回 content[0]（dict 或原样）。"""
        res = self._rpc("tools/call", {"name": name, "arguments": args}, timeout=self.timeout)
        blocks = res.get("content") or []
        if not blocks:
            return res.get("structuredContent")
        blk = blocks[0]
        if "text" in blk:
            try:
                return json.loads(blk["text"])
            except json.JSONDecodeError:
                return blk["text"]
        return blk                                                   # 图片块（含 base64 data / path）

    def call(self, op: str, *, session: str, document: Any = None, timeout: float | None = None,
             **fields: Any) -> dict:
        """走 ocs_execute 发一个控制操作。"""
        req = {"op": op, "request_id": f"ocads-{self._serial + 1}"}
        req.update(fields)
        if document is not None:
            req["document_id"] = document
        args: dict[str, Any] = {"ocs_session_id": session, "request": req}
        if document is not None:
            args["document_id"] = document
        try:
            return self.tool("ocs_execute", args) or {}
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"{op} 调用失败：{exc}") from exc

    def read(self, op: str, *, session: str, document: Any = None,
             parameters: dict | None = None) -> dict:
        args: dict[str, Any] = {"ocs_session_id": session, "op": op}
        if document is not None:
            args["document_id"] = document
        if parameters:
            args["parameters"] = parameters
        return self.tool("ocs_read", args) or {}

    # ---------------------------------------------------------------- 会话 / 弹窗
    def session(self, launch_if_none: bool = True, *, retries: int = 20) -> dict:
        """拿会话信息（session_id / 弹窗 / 文档列表）。

        编辑器冷启动期间 ``ocs_sessions`` 可能回一段非 JSON 的进度文本，这里重试到超时为止。
        """
        last = ""
        for _ in range(max(retries, 1)):
            text = self.tool("ocs_sessions", {"launch_if_none": launch_if_none})
            if isinstance(text, str):
                last = text[:200]
                time.sleep(1.0)
                continue
            if isinstance(text, dict) and "sessions" in text:
                text = text["sessions"]
            if text:
                return text[0] if isinstance(text, list) else text
            time.sleep(0.5)
        raise MCPError(f"拿不到编辑器会话（最后返回：{last!r}）")

    def dismiss_modals(self, session: str, *, allowlist: Iterable[str] | None = None,
                       max_rounds: int = 6) -> dict:
        """关掉挡路的弹窗，返回 ``{"closed": [...], "left": 名字或None}``。

        只关白名单内的；其它弹窗原样留着并在 ``left`` 里报出来（不替用户做决定）。
        """
        allowed = set(allowlist or MODAL_ALLOWLIST)
        closed: list[str] = []
        modal = self.session()["modal"] if isinstance(self.session(), dict) else None
        for _ in range(max_rounds):
            if not modal:
                break
            if modal not in allowed:
                return {"closed": closed, "left": modal}
            self.call("action", session=session, name="close_modal")
            closed.append(modal)
            self.closed_modals.append(modal)
            modal = (self.session() or {}).get("modal")
        return {"closed": closed, "left": modal or None}

    def prepare_document(self, *, new: bool = True, document: Any = None) -> dict:
        """清弹窗 → 选定/新建图纸并激活。返回 ``{"session","document","modals","documents"}``。"""
        info = self.session()
        sid = info["session_id"]
        modals = self.dismiss_modals(sid)
        docs = info.get("documents") or []
        did = document
        if did is None:
            existing = next((d["id"] for d in docs if not d.get("start")), None)
            did = existing
        if did is None and new:
            res = self.call("new", session=sid)
            did = (res.get("state") or {}).get("document_id")
            if did is None:
                raise MCPError(f"new 没建出图纸（弹窗？）：{json.dumps(res, ensure_ascii=False)[:200]}")
        if did is not None:
            self.call("activate", session=sid, document=did)
        return {"session": sid, "document": did, "modals": modals,
                "documents": [(d["id"], d.get("title"), bool(d.get("start")))
                              for d in (self.session().get("documents") or [])]}

    # ---------------------------------------------------------------- 画 / 读
    def run_many(self, commands: Iterable[str], document: Any, *, session: str,
                 strict: bool = False, esc_on_waiting: bool = True) -> dict:
        """逐条跑完整命令。

        注意：MCP 通道里 ``run`` 对"还能继续吃输入"的命令会回 ``waiting_input``
        （SPLINE/ARC 这些结尾没有显式终止符的都是），但**实体已经建出来了**。
        这里默认补一发 ``cancel``（等价按 Esc）把命令干净收尾，按"已完成"计。
        """
        ok, failed, no_op, waiting = 0, [], [], []
        for cmd in commands:
            res = self.call("run", session=session, document=document, cmd=cmd)
            status = res.get("status")
            if status == "waiting_input" and esc_on_waiting:
                self.call("cancel", session=session, document=document)
                waiting.append(cmd)
                status = "completed"
            if status in ("completed", "ok", None):
                ok += 1
                if not res.get("changes"):
                    no_op.append(cmd)
            else:
                failed.append({"cmd": cmd, "status": res.get("status"), "code": res.get("code")})
                if strict:
                    raise MCPError(f"命令失败：{cmd} → {res.get('code')}")
        return {"ok": ok, "failed": failed, "no_op": no_op,
                "waiting_input": len(waiting)}

    def handles(self, document: Any, *, session: str) -> list[str]:
        recs = self.read("records", session=session, document=document,
                         parameters={"collection": "entities"})
        return [e["handle"] for e in recs.get("records", []) if e.get("handle")]

    def measure(self, handles: Iterable[str], document: Any, *, session: str) -> dict:
        """内核量测（长度、面积、包围盒、质量特性）——只有 GUI MCP 有。"""
        hs = list(handles)
        if not hs:
            raise MCPError("measure 至少要一个句柄")
        return self.read("measure", session=session, document=document, parameters={"handles": hs})

    def set_properties(self, handle: str, updates: list[dict], document: Any, *,
                       session: str, collection: str = "entities") -> dict:
        """改记录属性（实体 / 对象 / header）。

        ``collection`` 是**必填**参数（上游要求），``updates`` 形如
        ``[{"path": "/common/color", "value": {"Index": 1}}]``。
        注意颜色值要的是序列化枚举（``{"Index": n}`` / ``"ByLayer"``），
        不是 ``"Red"``——工具层的 ``tools.set_properties`` 会帮忙转换。
        """
        return self.call("set_properties", session=session, document=document,
                         collection=collection, handle=handle, updates=updates)

    def zoom_extents(self, document: Any, *, session: str) -> dict:
        """缩放到图形范围。

        上游的 ``zoom_extents`` 是个 action（走交互式 ZOOM），实测会回 ``waiting_input``
        但**视图已经变了**，所以这里不把 waiting_input 当失败。
        """
        res = self.call("action", session=session, document=document, name="zoom_extents")
        if res.get("status") == "waiting_input":
            self.call("cancel", session=session, document=document)
        return res

    def capture(self, out_png: str, document: Any, *, session: str, target: str = "viewport",
                max_size: int = 1600, zoom: bool = True) -> dict:
        """原厂渲染截图（OCS 自己的渲染器）。上游把图写到临时文件并回 path，这里搬到 out_png。"""
        if zoom:
            self.zoom_extents(document, session=session)
        blk = self.tool("ocs_capture", {"ocs_session_id": session, "document_id": document,
                                        "target": target, "max_size": max_size})
        inner = blk.get("result") if isinstance(blk, dict) else None
        data = (blk or {}).get("data") or (inner or {}).get("data")
        path = (blk or {}).get("path") or (inner or {}).get("path")
        if data:
            with open(out_png, "wb") as fh:
                fh.write(base64.b64decode(data))
        elif path:
            shutil.copyfile(path, out_png)      # 上游把图写到临时文件并回 path
        else:
            raise MCPError(f"capture 返回里没有图：{json.dumps(blk, ensure_ascii=False)[:200]}")
        return {"png": os.path.abspath(out_png), "bytes": os.path.getsize(out_png),
                "target": target}

    def save(self, path: str, document: Any, *, session: str) -> dict:
        return self.call("save", session=session, document=document, path=os.path.abspath(path))

    # ---------------------------------------------------------------- 收尾
    def drain_stderr(self, limit: int = 800) -> str:
        if not self.proc or not self.proc.stderr:
            return ""
        try:
            return (self.proc.stderr.read() or "")[-limit:] if self.proc.poll() is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        for handle in ("stdin", "stdout", "stderr"):
            fh = getattr(self.proc, handle, None)
            try:
                if fh and not fh.closed:
                    fh.close()
            except Exception:  # noqa: BLE001
                pass
        self._kill_group()

    def _kill_group(self) -> None:
        """连同 xvfb-run 派生的 Xvfb 一起收掉，别留孤儿进程。"""
        if self.proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                self.proc.wait(timeout=8)
                return
            except subprocess.TimeoutExpired:
                continue

    def __enter__(self) -> "UpstreamMCP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
