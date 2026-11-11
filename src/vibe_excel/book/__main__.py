"""以 `python -m vibe_excel.book` 导出离线 book HTML。

参数解析与渲染都在 `vibe_excel.book.cli`，这里只负责进程退出码。
"""

from .cli import main

raise SystemExit(main())
