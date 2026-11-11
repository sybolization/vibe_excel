"""以 `python -m vibe_excel` 启动控制面 CLI。

真正的参数解析与业务分发都在 `vibe_excel.cli`，这里只做入口转发。
"""

from vibe_excel.cli import main

main()
