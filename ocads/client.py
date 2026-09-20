"""OpenCADStudio 无头自动化通道（``--serve``）的 Python 封装。

支持两种模式：
- **stdio**：``OpenCADStudio --serve``，一问一答的 JSON 行协议，进程随连接存活；
- **tcp**：``OpenCADStudio --serve --port N``，连本机 ``127.0.0.1:N``。

一个 ``ServeSession`` 就是一条连接 / 一个进程，里面保持"当前图纸"状态：
``new`` 之后可以连续 ``run`` 任意多条命令，最后 ``save`` 落盘。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Any, Iterable

__all__ = ["ServeError", "ServeSession", "find_binary", "discover"]

#: 找不到二进制时按顺序探测这些位置（本机编译产物优先）
CANDIDATE_PATHS = (
    os.environ.get("OPENCADSTUDIO_BIN", ""),
    "/public/ProjectCollection/2026_9/OpenCADStudio/target/release/OpenCADStudio",
    os.path.expanduser("~/.local/bin/OpenCADStudio"),
    "/usr/local/bin/OpenCADStudio",
    "/opt/OpenCADStudio/OpenCADStudio",
)


class ServeError(RuntimeError):
    """无头通道出错：进程起不来、协议错乱、命令被拒。"""


def find_binary() -> str:
    """定位 OpenCADStudio 可执行文件，找不到抛 ServeError。"""
    for path in CANDIDATE_PATHS:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which("OpenCADStudio")
    if found:
        return found
    raise ServeError(
        "找不到 OpenCADStudio 可执行文件；设置环境变量 OPENCADSTUDIO_BIN 指向它，"
        "或把 OpenCADStudio 放进 PATH。"
    )


class ServeSession:
    """``OpenCADStudio --serve`` 的一条会话（上下文管理器）。

    用法::

        with ServeSession() as s:
            s.request("new")
            print(s.run("CIRCLE 0,0 10"))      # 单条命令
            print(s.run_all(["LINE 0,0 10,0"]))  # 批量
            print(s.entities())
            s.save("/tmp/a.dxf")
    """

    def __init__(self, binary: str | None = None, port: int | None = None,
                 timeout: float = 120.0, cwd: str | None = None):
        self.binary = binary or find_binary()
        self.port = port
        self.timeout = timeout
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.ready: dict[str, Any] = {}
        self._rid = 0
        self._start()

    # ---------------------------------------------------------------- 生命周期
    def _start(self) -> None:
        args = [self.binary, "--serve"]
        if self.port:
            args += ["--port", str(self.port)]
        if self.port:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, cwd=self.cwd, text=True)
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise ServeError(f"--serve --port {self.port} 没能在 30 秒内监听")
            self.sock.settimeout(self.timeout)
            self._reader = self._writer = self.sock.makefile("rw", encoding="utf-8", newline="\n")
        else:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, text=True, bufsize=1,
            )
            self._reader = self.proc.stdout
            self._writer = self.proc.stdin
        first = self._readline()
        if not first:
            raise ServeError(f"OpenCADStudio --serve 没有握手就退出了：{self.drain_stderr()}")
        self.ready = json.loads(first)

    def close(self) -> None:
        """收干净：写端、读端、socket、进程，一个都别留。"""
        for handle in (getattr(self, "_writer", None), getattr(self, "_reader", None)):
            try:
                if handle is not None and not handle.closed:
                    handle.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc and self.proc.stderr:
            try:
                self.proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "ServeSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def drain_stderr(self, limit: int = 800) -> str:
        if not self.proc or not self.proc.stderr:
            return ""
        try:
            if self.proc.poll() is None:
                return ""
            return (self.proc.stderr.read() or "")[-limit:]
        except Exception:  # noqa: BLE001
            return ""

    # ---------------------------------------------------------------- 协议
    def _readline(self) -> str:
        try:
            line = self._reader.readline()
        except socket.timeout as exc:  # type: ignore[attr-defined]
            raise ServeError("等待 OpenCADStudio 响应超时") from exc
        return line.strip() if line else ""

    def send(self, payload: dict) -> dict:
        """发一个**原样**的请求体（协议 1 的字段是平铺的，不能套成 ``{"op": ...}``）。"""
        try:
            self._writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._writer.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ServeError(f"通道已断：{self.drain_stderr()}") from exc
        raw = self._readline()
        if not raw:
            raise ServeError(f"没有响应，进程可能已退出：{self.drain_stderr()}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServeError(f"响应不是 JSON：{raw[:200]}") from exc

    def control(self, op: str, *, document_id: Any = None, client_id: str = "ocads",
                **fields: Any) -> dict:
        """协议 1：走**控制面**（和 ``--mcp`` 同一套操作），字段平铺。

        这一层比旧 op 强：``measure``（内核量测）、``commands``（命令清单）、
        ``records``/``properties``/``history``、``capture`` 都在这里。返回原样 dict，
        失败判定用 ``ok``/``status``（``code`` 会是 ``command_busy`` /
        ``document_required`` / ``gui_required`` / ``stale_state`` 这类）。
        """
        self._rid += 1
        payload: dict[str, Any] = {"protocol": 1, "request_id": f"ocads-{self._rid}", "op": op}
        if document_id is not None:
            payload["document_id"] = document_id
        if client_id:
            payload["client_id"] = client_id
        payload.update({k: v for k, v in fields.items() if v is not None})
        return self.send(payload)

    def control_must(self, op: str, **kwargs: Any) -> dict:
        """同 ``control``，但失败就抛错，并把上游的 code 带在消息里。"""
        res = self.control(op, **kwargs)
        if not res.get("ok"):
            code = res.get("code") or res.get("status") or "failed"
            hint = ""
            if code == "command_busy":
                hint = "（有交互命令还没结束：先发 cancel 或用旧格式的 run 顶掉它）"
            elif code == "document_required":
                hint = "（这条操作必须带 document_id，先用 new/open 拿一个）"
            elif code == "gui_required":
                hint = "（无窗口环境做不了：capture 请走 mcp 引擎）"
            elif code == "stale_state":
                hint = "（修订号过期：先读一次 state 再改）"
            raise ServeError(f"{op} 被拒：{code} - {res.get('error') or ''}{hint}")
        return res

    def request(self, op: str, **params: Any) -> dict:
        """发一条**旧格式** op（``{"op": ...}``），返回响应 dict（不抛错）。"""
        payload = {"op": op}
        payload.update({k: v for k, v in params.items() if v is not None})
        try:
            self._writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._writer.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ServeError(f"通道已断（{op}）：{self.drain_stderr()}") from exc
        raw = self._readline()
        if not raw:
            raise ServeError(f"{op} 没有响应，进程可能已退出：{self.drain_stderr()}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServeError(f"{op} 的响应不是 JSON：{raw[:200]}") from exc

    def must(self, op: str, **params: Any) -> dict:
        """发一条 op，失败就抛错（避免"静默失败"）。"""
        res = self.request(op, **params)
        if not res.get("ok"):
            raise ServeError(f"{op} 被拒：{json.dumps(res, ensure_ascii=False)[:300]}")
        return res

    # ---------------------------------------------------------------- 常用动作
    def new(self) -> dict:
        return self.must("new")

    def run(self, cmd: str) -> dict:
        """跑一条**完整**命令（整行，含所有提示答案）。

        注意：``--serve`` 不支持交互式续行，``PLINE a b c`` 这种缺结尾的写法会
        停在 waiting_input，下一条命令会被当成新的命令行。
        """
        res = self.must("run", cmd=cmd)
        if res.get("status") == "waiting_input":
            raise ServeError(
                f"命令没走完就停在交互状态：{cmd!r}，"
                f"当前提示 {res.get('command', {})}；--serve 不能续行，请把整行答案写全。"
            )
        return res

    def run_all(self, cmds: Iterable[str], strict: bool = True) -> list[dict]:
        """批量跑命令，返回每条的结果（与输入一一对应）。"""
        out = []
        for cmd in cmds:
            try:
                out.append(self.run(cmd))
            except ServeError as exc:
                if strict:
                    raise
                out.append({"ok": False, "cmd": cmd, "error": str(exc)})
        return out

    def entities(self) -> dict:
        return self.must("entities")

    def new_document(self) -> int:
        """协议 1 建新图，返回 document_id（无头下没有启动弹窗挡路）。"""
        res = self.control_must("new")
        state = res.get("state") or {}
        did = state.get("document_id")
        if did is None:
            raise ServeError(f"new 没给出 document_id：{json.dumps(res, ensure_ascii=False)[:200]}")
        return int(did)

    def measure(self, handles: Iterable[str], document_id: Any = None) -> dict:
        """内核量测（长度/面积/包围盒/质量特性）——协议 1 提供，无头可用。"""
        hs = list(handles)
        if not hs:
            raise ServeError("measure 至少要一个带坐标的句柄")
        return self.control_must("measure", document_id=document_id, handles=hs)

    def command_manifest(self, name: str | None = None) -> dict:
        """命令清单（含批处理写法示例）——协议 1 提供，无头可用。"""
        return self.control_must("commands", name=name)

    def query(self, **params: Any) -> dict:
        """旧格式的内核查询：type/layer/handles/近邻/包含/包围盒/交点。

        - ``query(intersections=[h1, h2])`` → 真交点 + 参数（cadkernel::geom2d::intersect）
        - ``query(near=[x, y])`` → 最近实体 + distance + 参数
        - ``query(detail="full")`` → 每个实体的 bounds 与完整属性

        面积/长度这类量测不在这里：它属于**协议 1**，见 :meth:`measure`。
        """
        return self.must("query", **params)

    def intersections(self, first: str, second: str) -> dict:
        """两个句柄的真实交点（无头可用的内核能力）。"""
        return self.must("query", intersections=[first, second])

    def records(self, **params: Any) -> dict:
        """完整数据库记录（实体属性、符号表、header 等）。"""
        return self.must("records", **params)

    def near(self, x: float, y: float, **params: Any) -> dict:
        """离某点最近的实体（带内核算出的距离）。"""
        return self.must("query", near=[x, y], **params)

    def open(self, path: str) -> dict:
        if not os.path.isfile(path):
            raise ServeError(f"图纸不存在：{path}")
        return self.must("open", path=os.path.abspath(path))

    def save(self, path: str) -> dict:
        if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
            raise ServeError(f"目标目录不存在：{os.path.dirname(path)}")
        return self.must("save", path=os.path.abspath(path))


def discover(binary: str | None = None, timeout: float = 30.0) -> dict:
    """探测环境：二进制路径、版本、无头通道能力。"""
    info: dict[str, Any] = {"binary": None, "version": None, "headless": False, "error": None}
    try:
        info["binary"] = binary or find_binary()
    except ServeError as exc:
        info["error"] = str(exc)
        return info
    try:
        proc = subprocess.run([info["binary"], "--version"], capture_output=True,
                              text=True, timeout=timeout)
        info["version"] = (proc.stdout or proc.stderr).strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"--version 失败：{exc}"
    try:
        with ServeSession(binary=info["binary"], timeout=timeout) as s:
            info["headless"] = bool(s.ready.get("ready"))
            info["capabilities"] = s.request("capabilities")          # 旧格式
            try:
                # 协议 1 走的才是控制面：measure / commands / records 这些都在那儿
                res = s.control("capabilities")
                info["protocol_1"] = bool(res.get("ok"))
                info["control_capabilities"] = (res.get("editor") or {}).get("capabilities") or res
            except ServeError as exc:
                info["protocol_1"] = False
                info["protocol_1_error"] = str(exc)
    except ServeError as exc:
        info["error"] = str(exc)
    return info
