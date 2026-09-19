---
name: opencadstudio
description: 用 OpenCADStudio 生成、校验、导出和预览 CAD 图纸（DXF/DWG）。当需要“用 CAD 画某样东西”“生成 dxf/dwg 文件”“把图纸转格式”“看图纸长什么样”“批量改图/出图”，或要排查 OpenCADStudio 无头自动化报错时使用。不用于普通矢量绘图（那用 SVG/HTML 更合适），也不用于 3D 网格建模（STL 之外的通用建模另说）。
---

# OpenCADStudio 无头 CAD 工作流

把“一句话 → 真 CAD 文件 → 能看能验”这套流程跑通。**核心价值不是画得像，而是能验真**：
文件是真 DWG/DXF 编解码产物、几何能拿内核量测、命令结果能回读比对。

## 0. 先选引擎，再开始

| 引擎 | 用什么 | 强项 | 弱项 |
|---|---|---|---|
| `serve`（默认） | 上游 `--serve` JSON 行协议 | 快（一次连接跑完整批）、无窗口、稳 | **没有量测**、**没有原厂截图**、TEXT/HATCH 静默 no-op、不支持交互步骤 |
| `mcp` | 上游 `--mcp`（GUI 控制面，无显示时自动套 `xvfb-run`） | **内核量测**、**原厂渲染截图**、`set_properties` 线上色、**TEXT 实测可建**（交互步骤）、FILLET/TRIM 这类交互命令 | 慢一些（每条一个往返）、要 xvfb/DISPLAY |

起步自检：

```
ocads_info                     # 或：python3 -m ocads.tools info
```

返回里关心 `binary` / `version` / `headless` / `roots`。

- `headless=false` → `serve` 起不来，先看 `error`，别继续猜。
- 二进制缺失 → 设 `OPENCADSTUDIO_BIN` 指到编译产物。
- 写入被拒 → 路径不在 `OCADS_ROOTS` 内，换目录或加环境变量。

### mcp 引擎的三个必知坑（都踩过，实测解法）

1. **启动弹窗会挡住建图**：`AssocPrompt` → `DonationPrompt`，不清掉的话 `{"op":"new"}`
   返回 ok 却建不出图纸（上游 issue #1349 描述过同一现象）。解法：`{"op":"action",
   "name":"close_modal"}`（比发 `cancel`/Esc 精准）。本工具只自动关白名单里的
   `AssocPrompt / DonationPrompt / UpdateNotice / About`，其它弹窗**如实上报不代关**。
2. **读写都要带 `document_id`**：默认盯"活动标签页"，新建完活动页可能还停在开始页，
   不带 id 会读到空图纸。
3. **`waiting_input` 不等于失败**：SPLINE/ARC 这类"还能继续吃输入"的命令、
   以及 `zoom_extents`，实测都回 `waiting_input` **但实体/视图已经生效**。
   本工具默认补一发 `cancel`（Esc）收尾并按完成计。

### 交互步骤的输入协议（实测，踩过才写）

`start` 回 `state.command`，里面 `accepts` / `options` / `prompt` / `input_example` 就是要喂什么：

| 步骤类型 | 怎么喂 | 坑 |
|---|---|---|
| 选点 | `{"op":"input","kind":"point","point":[x,y,0],"space":"wcs"}` | **点是数组**，写成 `x`/`y` 字段会回 `Missing point for input` |
| 敲数字（高度/角度） | `{"op":"input","kind":"text","text":"5"}` | 是 **text** 不是 token；不给就直接 `kind:"enter"` 取默认值 |
| 关键字（`J`/`ST`/`C`） | `{"op":"input","kind":"token","text":"C"}` | 负载字段名是 **text**，写成 `token` 会回 `Missing text for input` |
| 回车 | `{"op":"input","kind":"enter"}` | 吃掉默认值 |

**写文字（TEXT）的完整六步**（`run` 做不到，批处理里它是静默 no-op）：

```
start  cmd=TEXT
input  kind=point point=[x,y,0]            # 起点
input  kind=text  text="<高度>"             # 回车则用 kind=enter 取默认
input  kind=text  text="<旋转角>"
action name=text_input value="<内容>"       # 这一步之后命令已结束、画布编辑器打开
action name=text_commit
```

第 4 步之后 `state.command` 会变成 `null`——**别用"还有没有 command"判断成败**，
去数实体里有没有 `Text`。本工具封装成 `mcp.add_text(...)`，返回 `texts` 计数。

## 1. 标准闭环（四步，缺一步就等于没做完）

1. **生成**：`ocads_run`，命令按顺序给，一行一条**完整**命令，最后 `save_path` 落盘。
2. **回读校验**：`ocads_read(op="entities")` 打开刚存的文件，比对类型分布和数量
   —— 和上一步的 `summary` 必须一致；`no_op` 列表里的命令要逐个解释。
3. **抽查几何**：`ocads_read` 用自己的内核能力验，别用“看起来对”代替：
   - `op="query"` + `parameters={"type":"Circle","detail":"full"}` → 半径、圆心、bounds
   - `op="intersections"` + `parameters={"handles":["63","64"]}` → 真交点坐标（内核算的）
   - `op="near"` + `parameters={"point":[0,0]}` → 最近实体 + 距离
   > 面积/长度那种 `measure` **无头模式没有**（GUI MCP 专属），别去试。
4. **出图验收**：`ocads_preview` 出 PNG，看图（这一步是给人和给 AI 自己看的）。

> 只跑第 1 步就汇报“画好了”，是不合格的。第 2、3 步是这套流程唯一比“让模型直接吐 DXF 文本”强的地方。

## 2. 命令语法（实测语义，别按 AutoCAD 直觉猜）

`ocads_run` 的 `commands` 必须是**整行**——所有提示答案一次给全，空格分隔：

| 命令 | 写法 | 语义要点 |
|---|---|---|
| 直线 | `LINE 0,0 10,0` | 两点 |
| 圆 | `CIRCLE 5,5 3` | 圆心 半径 |
| 三点弧 | `ARC 0,0 5,5 10,0` | 起点 第二点 终点 |
| 椭圆 | `ELLIPSE 0,0 40,0 20` | 中心 **轴端点(绝对坐标!)** 另一半轴长 |
| 多段线 | `PLINE 0,0 10,0 10,10 C` | `C` 闭合；不闭合就去掉 |
| 样条 | `SPLINE 0,0 20,10 40,0 C` | 点是**拟合点**（曲线过点），`C` 闭合 |
| 矩形 | `RECTANG -5,-5 5,5` | 两个对角点 |
| 实心盘 | `DONUT 0 9 0,0` | 内径 外径 圆心；**内径 0 = 实心圆盘** |
| 圆环 | `DONUT 4 10 0,0` | 内外径都给才是环 |
| 实心块 | `SOLID2D 0,0 10,0 10,10 0,10` | 四点，三角形就把第 4 点重复 |

**最容易错的三个坑：**

- `ELLIPSE` 的第二个点是**绝对坐标**（不是相对偏移）：`ELLIPSE 10,10 20,10 5`
  = 中心 (10,10)、半长轴 10、半短轴 5。写成相对偏移会得到一个巨大的椭圆。
- `DONUT 0 外径` 生成的其实是“带宽度多段线”，**视觉半径 ≈ 外径/2**，不是外径。
  想要半径 5 的实心眼珠，写 `DONUT 0 10`。
- 尺寸单位随图纸 `$INSUNITS`，同一张图里自己保持一致；跨图复用先看 `ocads_read(op="header")`。

## 3. 无头模式的硬限制（踩过，别再试）

| 想做的事 | 结论 |
|---|---|
| `TEXT` / `MTEXT` / `HATCH` / `POLYGON`（**serve 引擎**） | **静默 no-op**：返回 completed 但一个实体都不加。要文字就切 mcp 引擎用六步流程（见 §0 末）；HATCH/POLYGON 尚未验证 |
| 面积 / 长度量测（`measure`） | 只有 mcp 引擎有（`--serve` 回 `unknown op: measure`）。用 `ocads_read(op="measure", engine="mcp", open_path=…)` 或 `ocads_run(engine="mcp")` 返回的 `measurements` |
| 交互式续行（先 `PLINE` 再一步步给点） | 不支持。`--serve` 每行都是独立完整命令；半截命令**不一定报错**（见下） |
| 半截命令（缺参数） | 更坑：`LINE 0,0` 是静默不加实体；`CIRCLE 0,0`（缺半径）**照样 added=1**，造出一个退化圆。所以永远别信 `added`，要回读几何 |
| 无头导出 PNG/PDF | `--export` 只认 `.dwg/.dxf`。要图：`ocads_capture`（mcp 引擎，**原厂渲染**）或 `ocads_preview`（serve 侧自绘，仅需 Pillow） |
| GUI / 上游 `--mcp` | ✅ **能用**（无显示时自动 `xvfb-run`）。卡住的原因从来不是"没窗口"，而是启动弹窗没关 + 没带 `document_id`（见上面三条坑） |
| 硬件加速 | 没独显时会降级软件渲染（llvmpipe），大图慢，别开一堆并发 |

`syntax_guard` 会提前警告第 1 行那类命令，`run_commands` 会把"该出实体却没出"的命令放进
`no_op`；看到 `warnings` / `no_op` 不要无视。

## 4. 怎么算“画对了”

- `entities` 数量 = 预期条数；`by_type` 分布符合预期（SPLINE 3 / ARC 8 / …）
- `no_op` 为空（该出实体的命令都出了；空转命令要么解释清楚，要么改写法）
- `skipped = 0`（预览器里没有画不出来的实体）
- `ocads_preview` 返回 `drawn` ≈ `entities`
- 要交付实体的场合：`engine="mcp"` 跑一遍，用 `measurements`（面积/长度/闭合）核对，再用 `ocads_capture` 出原厂渲染图
- 多轮迭代时给截图带 `if_changed=True`：画面没变就只回 `changed=false` + `reuse_previous`，
  别把同一张图反复读进上下文（省 token）；注意它是**逐格墨迹占比**比较，
  `threshold=0` 也不代表"任何像素差异都算变"（单格要先超过 `tile_eps`）
- 合图形状对得上：先画一眼轮廓、看出问题再补细节，**每轮都出图**，别盲改坐标

## 5. 工具清单

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `ocads_info` | 环境自检 | — |
| `ocads_run` | 跑命令、存图 | `commands[]`、`open_path`、`save_path` |
| `ocads_read` | 回读校验 | `op` = entities/records/query/intersections/near/layers/header/capabilities；**`measure` 需 `engine="mcp"`** |
| `ocads_capture` | **原厂渲染**截图 | `png_path`、`commands`/`dxf_path`、`target`、`view`、`if_changed`+`threshold` |
| `ocads_set_view` | 切视图（`home`/`extents`） | `name` |
| `ocads_set_properties` | 真改实体属性（线上色/换图层） | `handle` + `updates=[{"path":"/common/color","value":"Red"}]` |
| `ocads_export` | 无头转格式 | `src`、`dst`（只 `.dwg/.dxf`） |
| `ocads_preview` | DXF → PNG | `dxf_path`、`png_path`、`scale`、`colors` |

`colors` 示例：`{"SPLINE":"black","WATER":"blue","Circle":1}` —— 键可以是实体类型或图层名，
值可以是 ACI 索引或颜色名（black/red/yellow/green/cyan/blue/magenta/gray/orange/…）。
注意这只影响渲染，不改文件。

## 5.1 同类实践（参考，别重复造）

`helenkwok/ocs-webmcp`（MIT）把上游控制面接到浏览器 WebMCP（24 个工具、人工确认门、
乐观并发、录制 contact sheet），跑的是 web 版。它验证了同一套控制面在浏览器里的边界，
也把"只自动关捐赠弹窗、其它弹窗上报"这个更严的安全姿态写实了——本工具照抄了这条。
差异：它要浏览器 + wasm 构建，我们的 `mcp` 引擎要的是本机二进制（无浏览器）。

## 6. 安全边界

- 只能读写 `OCADS_ROOTS` 里的路径（默认 cwd + /tmp + home），越界会报错而不是硬写。
- 命令里不要拼外部输入（CAD 命令集里有删文件/打印类命令），需要批处理时用白名单。
- 单次会话有超时（默认 300s），长任务拆成多次 `ocads_run`，别开常驻。
- 许可证：OpenCADStudio 是 GPL-3.0。走子进程调用没问题，**不要把它的代码嵌进别的项目**。

## 7. 排错速查

| 症状 | 处理 |
|---|---|
| `命令没走完就停在交互状态` | 把该命令的提示答案补齐（点/选项），或换命令行能一次完成的写法 |
| `run` 返回 ok 但 `added=0` | 大概率撞上 no-op 命令（TEXT/HATCH/POLYGON），或命令缺参数被静默吞掉 |
| 明明"成功"但几何不对（半径/位置离谱） | 缺参数的命令会退化成功：用 `op="query", detail="full"` 读真实 radius/center/bounds |
| `unknown op: measure` | 无头没有这个能力，改用 intersections / near |
| `open` 成功但 entities 为空 | 文件里实体在 BLOCK 里（预览器暂不支持块展开） |
| 预览一片空白 | `scale` 太小或实体坐标跨度极大 → 调大 `scale` 或指定 `extents` |
| 进程起不来 | 看 `drain_stderr` 内容；确认 `--serve` 可用（`ocads_info`） |
