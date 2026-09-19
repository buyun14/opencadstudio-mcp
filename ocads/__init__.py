"""ocads —— 把 OpenCADStudio 的无头自动化通道包成 AI 能用的工具。

三层：

- :mod:`ocads.client`  无头 ``--serve`` 通道（进程 + JSON 行协议）
- :mod:`ocads.dxf`     DXF 解析与自绘渲染（看图 / 验真）
- :mod:`ocads.tools`   给 LLM 的动作：run / read / export / preview
"""

__all__ = ["client", "dxf", "tools"]
__version__ = "0.1.0"
