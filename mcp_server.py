#!/usr/bin/env python3
"""OpenCADStudio 无头 MCP server（stdio，零第三方依赖）。

为什么不用上游自带的 ``OpenCADStudio --mcp``：那个挂在**活的桌面编辑器**上，
需要真实窗口；服务器/容器里起不来（实测 Xvfb 下窗口创建失败，``new`` 建不出图）。
这个 server 走无头 ``--serve`` 通道，专门给没有显示器的环境用。

用法：

    python3 mcp_server.py            # 客户端用 stdio 拉起
    OCADS_ROOTS=/path/a:/path/b python3 mcp_server.py

协议：MCP 2024-11-05 ~ 2025-06-18 的 stdio 子集（initialize / tools/list /
tools/call / ping），单行 JSON-RPC 2.0。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ocads import tools  # noqa: E402

SERVER_NAME = "opencadstudio-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28")


def log(*parts: Any) -> None:
    print("[ocads-mcp]", *parts, file=sys.stderr, flush=True)


def reply(msg_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_error(msg_id: Any, code: int, message: str, data: Any = None) -> None:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": payload},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        version = want if isinstance(want, str) and want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        reply(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": ("用 ocads_info 先探测环境；用 ocads_run 跑 CAD 命令（每行必须是完整命令）；"
                             "用 ocads_read 回读校验（measure 可拿到内核量测）；"
                             "用 ocads_preview 出 PNG 看图。无头模式不支持交互续行，"
                             "TEXT/MTEXT/HATCH/POLYGON 会被静默忽略。"),
        })
        return

    if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
        return

    if method == "ping":
        reply(msg_id, {})
        return

    if method == "tools/list":
        reply(msg_id, {"tools": tools.tool_schemas()})
        return

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = tools.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            log("工具异常:", traceback.format_exc().strip().splitlines()[-1])
            reply(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                           "isError": True})
            return
        failed = isinstance(result, dict) and result.get("ok") is False
        reply(msg_id, {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result if isinstance(result, dict) else {"value": result},
            "isError": bool(failed),
        })
        return

    if method in ("resources/list", "prompts/list"):
        reply(msg_id, {"resources": []} if method.endswith("resources/list") else {"prompts": []})
        return

    if msg_id is not None:
        reply_error(msg_id, -32601, f"不支持的方法：{method}")


def main() -> int:
    log(f"启动，二进制：{os.environ.get('OPENCADSTUDIO_BIN') or '(自动探测)'}，"
        f"允许目录：{os.pathsep.join(tools.allowed_roots())}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("收到非 JSON 行，忽略:", line[:120])
            continue
        try:
            handle(msg)
        except Exception:  # noqa: BLE001
            log("处理消息崩了:", traceback.format_exc().strip().splitlines()[-1])
            if msg.get("id") is not None:
                reply_error(msg["id"], -32603, "内部错误，详见服务端 stderr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
