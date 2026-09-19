# opencadstudio-mcp

把 **OpenCADStudio** 的无头自动化通道包成 AI 能直接用的能力：**一个 MCP server（能力层）+ 一个 Skill（知识层）**。

> 一句话：让模型能“画得出真 DXF/DWG、验得了几何、看得见图”。

## 为什么不直接用上游自带的 `OpenCADStudio --mcp`

上游那个 MCP 挂在**活的桌面编辑器**上，需要真实窗口。服务器 / 容器 / CI 里起不来：
实测 Xvfb 下窗口创建失败，`ocs_sessions` 能连、但 `new` 建不出图纸（`no_document`），
`ocs_capture` 也就无从谈起。

这个 server 改走无头 `--serve`（stdin/stdout JSON 行协议），专治没有显示器的环境，
并且把上游无头模式缺的那一环——**看图**——自己补上了（自带 DXF 渲染器）。

| 能力 | 上游 GUI MCP | 本项目（无头 MCP） |
|---|---|---|
| 无显示器环境 | ❌ | ✅ |
| 生成 / 保存 / 回读 / 量测 | ✅ | ✅ |
| 无头转 DWG/DXF | — | ✅ |
| 无头看图（PNG） | ❌（要窗口截图） | ✅ 自绘 |
| 交互式命令续行（TEXT/HATCH 等） | ✅ | ❌（上游无头限制，见 SKILL） |

## 安装

```bash
python3 -m pip install -r requirements.txt       # 只依赖 Pillow
export OPENCADSTUDIO_BIN=/path/to/OpenCADStudio  # 不给就自动探测 PATH/常见位置
```

## 用法一：挂进 AstrBot

`data/mcp_server.json` 里加一条（与现有条目并列）：

```json
{
  "name": "opencadstudio",
  "command": "python3",
  "args": ["/public/ProjectCollection/2026_9/Astr-plugins/opencadstudio-mcp/mcp_server.py"],
  "env": {
    "OPENCADSTUDIO_BIN": "/public/ProjectCollection/2026_9/OpenCADStudio/target/release/OpenCADStudio",
    "OCADS_ROOTS": "/public/ProjectCollection/2026_9"
  }
}
```

再把 `skills/opencadstudio/` 复制（或软链）到 `AstrBot/data/skills/opencadstudio/`，
模型就同时有了「工具」和「怎么用工具」的知识。

## 用法二：其他 MCP 客户端（Claude / Codex / DSH）

把 stdio server 指向：

```json
{"mcpServers": {"opencadstudio": {"command": "python3",
  "args": ["/public/ProjectCollection/2026_9/Astr-plugins/opencadstudio-mcp/mcp_server.py"]}}}
```

## 用法三：当 Python 库用

```python
from ocads import tools

# 画 + 存
r = tools.run_commands([
    "PLINE 0,0 40,0 40,30 0,30 C",     # 每行必须是完整命令
    "CIRCLE 20,15 8",
    "DONUT 0 10 20,15",                # 内径 0 = 实心盘
], save_path="/tmp/box.dxf")
print(r["summary"], r["failed"], r["warnings"])

# 验真：回读 + 内核量测（handles 从 read_document("records") 里拿）
print(tools.read_document("entities", open_path="/tmp/box.dxf")["by_type"])
print(tools.read_document("measure", open_path="/tmp/box.dxf",
                          parameters={"handles": ["2A", "2B"]}))

# 看图
print(tools.preview("/tmp/box.dxf", "/tmp/box.png", colors={"CIRCLE": "red"}))
```

## 五个工具

| 工具 | 作用 |
|---|---|
| `ocads_info` | 探测二进制 / 版本 / 无头通道 / 允许目录 |
| `ocads_run` | 跑一批完整命令 → 可存 DXF/DWG，返回实体统计与失败清单 |
| `ocads_read` | `entities` / `records` / `query` / `measure`（内核量测）/ `layers` / `header` / `capabilities` |
| `ocads_export` | 一次性无头格式转换（只 `.dwg`/`.dxf`） |
| `ocads_preview` | DXF → PNG（自带渲染器，读文件本身，可当验收手段） |

工具 schema 定义在 `ocads/tools.py::tool_schemas()`，MCP server 和以后的 AstrBot 插件共用一份。

## 目录

```
ocads/client.py    --serve 通道封装（stdio / tcp、会话、错误不再静默）
ocads/dxf.py       DXF 解析 + Pillow 渲染（含 bulge、椭圆、样条插值、图层色）
ocads/tools.py     工具层（路径沙箱、语法守卫、5 个动作、schema）
mcp_server.py      stdio MCP server（零第三方依赖）
skills/opencadstudio/SKILL.md   知识层：命令语义 + 坑 + 闭环流程
examples/otter_draw.py          示例：画一只海獭并出图
tests/                          unittest（无需 pytest）
```

## 已知限制 / 路线图

- 预览器不展开 BLOCK/INSERT，遇到块引用里的实体只会统计到 `skipped`。
- `TEXT`/`HATCH` 在无头 `run` 下是上游静默 no-op（值得给上游提 issue）。
- 无头导出 PDF/PNG 上游未开放（`pdf_export.rs` 只被 GUI 调用）；本地渲染只是替代方案，
  真机有显示器时应优先用上游渲染器。
- 想给图纸上色目前只能“渲染时上色”（`preview(colors=…)`），改文件颜色要等上游把
  `set_properties` 开放给 `--serve`，或走 GUI MCP。

## 许可证

本项目仅通过**子进程**调用 OpenCADStudio（GPL-3.0），不链接其代码；本仓库自身可独立授权。
分发时请随附 OpenCADStudio 的许可证与源码获取方式。
