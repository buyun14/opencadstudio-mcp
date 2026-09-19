---
name: opencadstudio
description: 用 OpenCADStudio 生成、校验、导出和预览 CAD 图纸（DXF/DWG）。当需要“用 CAD 画某样东西”“生成 dxf/dwg 文件”“把图纸转格式”“看图纸长什么样”“批量改图/出图”，或要排查 OpenCADStudio 无头自动化报错时使用。不用于普通矢量绘图（那用 SVG/HTML 更合适），也不用于 3D 网格建模（STL 之外的通用建模另说）。
---

# OpenCADStudio 无头 CAD 工作流

把“一句话 → 真 CAD 文件 → 能看能验”这套流程跑通。**核心价值不是画得像，而是能验真**：
文件是真 DWG/DXF 编解码产物、几何能拿内核量测、命令结果能回读比对。

## 0. 开始前先自检

```
ocads_info                     # 或：python3 -m ocads.tools info
```

返回里关心的字段：`binary`（二进制路径）、`version`、`headless`（无头通道可用）、
`roots`（允许写入的目录白名单）。

- `headless=false` → 无头通道起不来，先看 `error`，别继续猜。
- 二进制缺失 → 设 `OPENCADSTUDIO_BIN` 指到编译产物（本机在
  `/public/ProjectCollection/2026_9/OpenCADStudio/target/release/OpenCADStudio`）。
- 写入被拒 → 路径不在 `OCADS_ROOTS` 内，换目录或加环境变量。

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
| `TEXT` / `MTEXT` / `HATCH` / `POLYGON` | **静默 no-op**：返回 completed 但一个实体都不加。要文字/填充必须走 GUI 或上游 MCP 的 `start`/`step` 交互流 |
| 面积 / 长度量测（`measure`） | 只有 GUI MCP 有，`--serve` 会直接回 `unknown op: measure`。无头验真改用 `intersections` / `near` / `detail="full"` |
| 交互式续行（先 `PLINE` 再一步步给点） | 不支持。`--serve` 每行都是独立完整命令；半截命令**不一定报错**（见下） |
| 半截命令（缺参数） | 更坑：`LINE 0,0` 是静默不加实体；`CIRCLE 0,0`（缺半径）**照样 added=1**，造出一个退化圆。所以永远别信 `added`，要回读几何 |
| 无头导出 PNG/PDF | 上游只开了 `.dwg/.dxf`（`pdf_export.rs` 只被 GUI 的 plot/print 调用）→ 用 `ocads_preview` 自绘 |
| GUI / 上游 `--mcp` | 需要真实窗口；服务器上（含 Xvfb）起不来，`ocs_sessions` 能连但 `new` 建不出图 |
| 硬件加速 | 没独显时会降级软件渲染（llvmpipe），大图慢，别开一堆并发 |

`syntax_guard` 会提前警告第 1 行那类命令，`run_commands` 会把"该出实体却没出"的命令放进
`no_op`；看到 `warnings` / `no_op` 不要无视。

## 4. 怎么算“画对了”

- `entities` 数量 = 预期条数；`by_type` 分布符合预期（SPLINE 3 / ARC 8 / …）
- `no_op` 为空（该出实体的命令都出了；空转命令要么解释清楚，要么改写法）
- `skipped = 0`（预览器里没有画不出来的实体）
- `ocads_preview` 返回 `drawn` ≈ `entities`
- 合图形状对得上：先画一眼轮廓、看出问题再补细节，**每轮都出图**，别盲改坐标

## 5. 工具清单

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `ocads_info` | 环境自检 | — |
| `ocads_run` | 跑命令、存图 | `commands[]`、`open_path`、`save_path` |
| `ocads_read` | 回读校验 | `op` = entities/records/query/intersections/near/layers/header/capabilities |
| `ocads_export` | 无头转格式 | `src`、`dst`（只 `.dwg/.dxf`） |
| `ocads_preview` | DXF → PNG | `dxf_path`、`png_path`、`scale`、`colors` |

`colors` 示例：`{"SPLINE":"black","WATER":"blue","Circle":1}` —— 键可以是实体类型或图层名，
值可以是 ACI 索引或颜色名（black/red/yellow/green/cyan/blue/magenta/gray/orange/…）。
注意这只影响渲染，不改文件。

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
