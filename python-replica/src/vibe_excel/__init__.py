"""带版本的 Excel 到 SQLite 工作流，配有独立的 Python 控制面。

本包是自包含的复刻版：把 Excel 输入物化为 SQLite 表，并按数据版本（revision）
记录会话（session）与运行（run）。这里只暴露版本号；控制面入口在
`vibe_excel.cli`，book/encoder/sqlmap 是从 store 派生出的只读视图。
"""

__version__ = "0.1.0"
