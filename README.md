# opencadstudio-mcp

把 **OpenCADStudio** 的无头自动化通道包成 AI 能直接用的能力：**一个 MCP server（能力层）+ 一个 Skill（知识层）**。

> 一句话：让模型能“画得出真 DXF/DWG、验得了几何、看得见图”。

## 两条引擎，而不是二选一

本项目把上游的两条自动化通道都包了，按需要切：

| | `serve` 引擎（默认） | `mcp` 引擎 |
|---|---|---|
| 上游接口 | `--serve` JSON 行协议 | `--mcp` GUI 控制面（无显示时自动 `xvfb-run`） |
| 速度 | 一次连接跑完整批，快 | 每条命令一个往返，慢一些 |
| 量测（面积/长度/质量特性） | ❌（`unknown op: measure`） | ✅ 几何核出的真值 |
| 原厂渲染截图 | ❌ | ✅ `ocads_capture` |
| TEXT / HATCH / POLYGON | ❌ 静默 no-op | ✅ 走 `start`/`input` 交互步骤 |
| 线上色（`set_properties`） | ❌ | ✅ |
| 自带渲染（Pillow 自绘） | ✅ `ocads_preview` | 不需要 |

> 曾经的错误结论：**"上游 MCP 无头跑不起来，因为有窗口依赖"**——错的。
> 真正的原因是两件事：① 启动弹窗（`AssocPrompt`/`DonationPrompt`）没关，导致
> `{"op":"new"}` 返回 ok 却建不出图纸（上游 issue **#1349** 描述过同一现象）；
> ② 读写没带 `document_id`，读到的是还停在开始页的活动标签。
> 用 `{"op":"action","name":"close_modal"}` 关弹窗 + 带 `document_id` 就通了，
> 无显示环境套 `xvfb-run` 即可（实测 44 条命令、44 实体、量测与截图全通）。

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
| `ocads_run` | 跑一批完整命令（`engine` 可选 serve/mcp）→ 可存 DXF/DWG、可顺手截图，返回实体统计、失败与空转清单 |
| `ocads_read` | `entities` / `records` / `query` / `intersections` / `near` / `layers` / `header` / `capabilities`；`measure` 需 `engine="mcp"` |
| `ocads_export` | 一次性无头格式转换（只 `.dwg`/`.dxf`） |
| `ocads_capture` | **原厂渲染**截图（mcp 引擎 + `ocs_capture`，截图前自动 `zoom_extents`） |
| `ocads_set_properties` | 真改实体属性（线上色 / 换图层），只有 mcp 引擎能做 |
| `ocads_preview` | DXF → PNG（自带 Pillow 渲染器，读文件本身，零外部依赖） |

工具 schema 定义在 `ocads/tools.py::tool_schemas()`，MCP server 和以后的 AstrBot 插件共用一份。

## 目录

```
ocads/client.py      --serve 通道封装（stdio / tcp、会话、错误不再静默）
ocads/mcp_client.py  上游 GUI MCP 通道（弹窗白名单关闭、document_id 路由、量测、原厂截图）
ocads/dxf.py         DXF 解析 + Pillow 渲染（含 bulge、椭圆、样条插值、图层色）
ocads/tools.py       工具层（路径沙箱、语法守卫、引擎切换、7 个工具、schema）
mcp_server.py        stdio MCP server（零第三方依赖）
skills/opencadstudio/SKILL.md   知识层：引擎选择 + 命令语义 + 坑 + 闭环流程
examples/otter_draw.py          示例：画一只海獭并出图
tests/                          unittest（无需 pytest）
```

## 参考与致谢

- 上游自动化契约讨论：HakanSeven12/OpenCADStudio issue **#1349**
- 同类实践：**helenkwok/ocs-webmcp**（MIT，浏览器 WebMCP 路线，24 个工具）。
  它验证了同一套控制面在浏览器里的边界，并且把"只自动关捐赠弹窗、其它弹窗上报"
  这个更严的安全姿态写实了 —— 本项目的 mcp 引擎照抄了这条（白名单：
  `AssocPrompt / DonationPrompt / UpdateNotice / About`）。

## 已知限制 / 路线图

- 预览器不展开 BLOCK/INSERT，遇到块引用里的实体只会统计到 `skipped`。
- `TEXT`/`HATCH` 在无头 `run` 下是上游静默 no-op（值得给上游提 issue）。
- 无头导出 PDF/PNG 上游未开放（`pdf_export.rs` 只被 GUI 调用）；本地渲染只是替代方案，
  真机有显示器时应优先用上游渲染器。
- 给图纸上色：`ocads_set_properties`（mcp 引擎）改的是图纸数据；`ocads_preview` 的
  `colors` 只是渲染时上色，不改文件。

## 许可证

本项目仅通过**子进程**调用 OpenCADStudio（GPL-3.0），不链接其代码；本仓库自身可独立授权。
分发时请随附 OpenCADStudio 的许可证与源码获取方式。
