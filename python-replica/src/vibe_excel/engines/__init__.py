"""内置的原版 Python 引擎；不依赖 TypeScript 仓库中的任何文件。

三个脚本各自独立运行：flow_parse.py 静态解析流程，materialize.py 把 Excel 落成表，
node_runner.py 调用单个节点函数。它们都只通过命令行/stdin/stdout 与上层交互。
"""
