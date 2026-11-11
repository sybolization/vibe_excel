"""以 `python -m vibe_excel.sqlmap` 校验血缘并导出离线界面。

参数解析与文件写出都在 `vibe_excel.sqlmap.cli`，这里只负责进程退出码。
"""

from .cli import main

raise SystemExit(main())
