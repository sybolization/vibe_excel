"""把这个命令丢到后台，立刻返回 job 名和日志路径。

    python _chat/bg.py "<command>"

命令自己跑，输出进 _chat/jobs/<job>/out.log，末尾一行 __DONE__ <退出码>。
超过 15 秒的活都该走这里，不要在前台干等。

实现上不在 .cmd 里拼命令：批处理文件按当前代码页解析，命令里带中文就会被打乱
（写进去是一串问号，读回来是乱码）。改成起一个小的 Python 监督进程，命令经环境变量
传过去——环境变量和命令行参数都是 Unicode，不过代码页这一关。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DONE = "__DONE__"

_SUPERVISOR = '''"""跑一条命令，把输出和退出码写进日志。由 bg.py 拉起，不给人直接调用。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DONE = "__DONE__"


def main() -> int:
    command = os.environ.get("BG_COMMAND", "")
    log = Path(os.environ.get("BG_LOG", "out.log"))
    workdir = os.environ.get("BG_WORKDIR") or None
    if not command:
        return 2
    with log.open("ab", buffering=0) as handle:
        code = subprocess.run(
            command,
            shell=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=workdir,
        ).returncode
        handle.write(f"\\n{DONE} {code}\\n".encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _next_job(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job = root / stamp
    suffix = 1
    while job.exists():
        job = root / f"{stamp}-{suffix}"
        suffix += 1
    job.mkdir(parents=True)
    return job


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print('用法：python _chat/bg.py "<command>"')
        return 2
    command = argv[1]
    here = Path(__file__).resolve().parent
    job = _next_job(here / "jobs")
    log = job / "out.log"
    supervisor = job / "run.py"
    supervisor.write_text(_SUPERVISOR, encoding="utf-8", newline="\n")

    env = dict(os.environ)
    # 子进程的 PATH 里先放「当前这个解释器」的目录：脱离出去以后，服务进程的 PATH
    # 和人的 shell 未必一样，不补这一下可能连 python 都找不到。
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # 中文 Windows 的默认输出编码是 GBK，日志会被读成乱码：要 UTF-8
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["BG_COMMAND"] = command
    env["BG_LOG"] = str(log)
    env["BG_WORKDIR"] = str(here.parent)

    flags = 0
    if os.name == "nt":
        # 必须是「新开一个控制台」而不是 DETACHED_PROCESS：实测 DETACHED 下子进程的
        # stdout 落不进文件（只剩哨兵那行），命令等于白跑。
        # 再叠加 BREAKAWAY_FROM_JOB：调用方（pi 的 bash 工具）收尾时会连带杀掉同 job
        # 对象里的进程，不跳出来命令就活不过这一轮。
        flags = 0x00000010 | 0x00000200 | 0x01000000

    subprocess.Popen(
        [sys.executable, str(supervisor)],
        creationflags=flags,
        cwd=str(here.parent),
        env=env,
        close_fds=True,
    )

    print(f"job {job.name}")
    print(f"log {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
