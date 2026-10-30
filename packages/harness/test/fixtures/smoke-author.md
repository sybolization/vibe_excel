# 冒烟提示词（harness 的 author 通路）

你在一台 Windows 机器上工作，当前目录是这个项目的程序工作区。
可用工具只有 pi 内置的 bash / read / write / edit。不要执行任何 git 命令。

## 环境事实

- Python 只能用这个绝对路径（系统 `python` 是坏的商店壳）：`{{PYTHON}}`
- 固定业务日期 `AS_OF={{ASOF}}`

## 这轮要做的事

1. 写一个 `hello.py`，内容：读环境变量 `VIBE_EXCEL_AS_OF`，打印 `hello <那个值>`（没有就打印 `hello unknown`）。
2. 用上面那个解释器真的跑一遍，确认输出是 `hello {{ASOF}}`。
3. 只写这一个文件，不要碰别的。

完成后用一两句话说明你实际看到的输出（要贴原文，不要复述）。
