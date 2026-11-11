"""以 `python -m vibe_excel.encoder` 运行编码 CLI。

参数解析、记录与语料库操作都在 `vibe_excel.encoder.cli`，这里只做入口转发。
"""

from .cli import main

main()
