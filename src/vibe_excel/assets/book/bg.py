"""把这个命令丢到后台，立刻返回 job 名和日志路径。

    python _chat/bg.py "<command>"

命令自己跑，输出进 _chat/jobs/<job>/out.log，末尾一行 __DONE__ <退出码>。
超过 15 秒的活都该走这里，不要在前台干等。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DONE = "__DONE__"


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
    workdir = str(here.parent)

    if os.name == "nt":
        script = job / "run.cmd"
        body = "\r\n".join(
            [
                "@echo off",
                f'{command} >> "{log}" 2>&1',
                f'echo {DONE} %ERRORLEVEL% >> "{log}"',
                "",
            ]
        )
        script.write_text(body, encoding="utf-8", newline="")
        # 三个标志缺一不可：脱离控制台、独立进程组、跳出父进程的 job 对象。
        # 少了最后那个，调用方（比如 pi 的 bash 工具）收尾时会连它一起杀掉。
        flags = 0x00000008 | 0x00000200 | 0x01000000
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=flags,
            cwd=workdir,
            close_fds=True,
        )
    else:
        script = job / "run.sh"
        body = f'({command}) >> "{log}" 2>&1\necho {DONE} $? >> "{log}"\n'
        script.write_text(body, encoding="utf-8", newline="\n")
        subprocess.Popen(
            ["/bin/sh", str(script)],
            start_new_session=True,
            cwd=workdir,
        )

    print(f"job {job.name}")
    print(f"log {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
