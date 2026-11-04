"""按名字调用程序里的一个函数。

流里的一个节点 = 一个函数。这个文件只做四件事：
  1. 按路径把目标文件当模块加载（不要求它是包，也不要求目录里有 __init__.py）；
  2. 从命令行拿 key=value 形式的入参；
  3. 只把函数**签名里真的接受**的入参传进去（多余的不传，缺的也不硬塞）；
  4. 调用它，把返回值（如果是 dict）打到 stdout，异常原样抛给调用方。

为什么要这样：入参协议（db / inputs / out / as_of）由平台定义，函数按需声明。
这样同一个文件里的多个函数可以共用一个上下文，也不用为每个节点写一段 argparse。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import traceback


def load_module(path: str):
    spec = importlib.util.spec_from_file_location("_vibe_node_target", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：目标文件里若有 dataclass / 类型注解自引用，需要模块已在 sys.modules 里。
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.rsplit("/", 1)[0] or "."))
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] in ("", ".") or sys.path and sys.path[0] == str(path.rsplit("/", 1)[0] or "."):
            sys.path.pop(0)
    return module


def parse_pairs(argv: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if not key.startswith("--"):
            raise SystemExit(f"参数形状不对（期望 --key value）：{key}")
        if index + 1 >= len(argv):
            raise SystemExit(f"参数 {key} 没有值")
        values[key[2:].replace("-", "_")] = argv[index + 1]
        index += 2
    return values


def call_with_signature(func, values: dict[str, str]):
    signature = inspect.signature(func)
    accepts_extra = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    kwargs = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in values:
            kwargs[name] = values[name]
        elif parameter.default is inspect.Parameter.empty and parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise SystemExit(
                f"函数 {func.__name__} 需要参数 {name}，但平台没有提供；"
                f"可用的入参：{', '.join(sorted(values))}"
            )
    if accepts_extra:
        for name, value in values.items():
            kwargs.setdefault(name, value)
    return func(**kwargs)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("用法：node_runner.py <文件> <函数名> [--key value ...]")
    path, name = argv[0], argv[1]
    values = parse_pairs(argv[2:])

    module = load_module(path)
    func = getattr(module, name, None)
    if func is None or not callable(func):
        raise SystemExit(f"{path} 里没有可调用的 {name}")
    if not inspect.isfunction(func):
        raise SystemExit(f"{path}:{name} 不是函数（节点必须指向一个函数）")

    result = call_with_signature(func, values)
    if isinstance(result, dict):
        json.dump(result, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
