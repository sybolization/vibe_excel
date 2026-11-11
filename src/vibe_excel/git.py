"""对 git 命令行的薄封装，所有仓库操作都走这里。

每个函数只拼一条 git 命令并解释其退出码；不在这里做业务判断（是否可发布由 session 决定）。
统一关闭 gpg 签名、autocrlf 与分页器，保证在无交互环境里行为稳定。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vibe_excel.util import VibeError

# 提交与合并都带上这个尾注，便于追溯工具生成的变更。
COAUTHOR = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


def git(
    repo: str | Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """执行一条 git 命令；check=True 时非零退出码转成带 stderr 的 VibeError。"""
    # --no-pager：非交互环境下 pager 会挂住进程。
    result = subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise VibeError(f"git {' '.join(args)} 执行失败: {result.stderr.strip() or result.stdout.strip()}")
    return result


def init_repo(repo: str | Path, main_branch: str = "main") -> None:
    """初始化程序仓库：建 main 分支、固定本地身份与换行策略、写忽略与 README，并提交。"""
    root = Path(repo)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, ["init", "-b", main_branch])
        git(root, ["config", "user.name", "Vibe Excel"])
        git(root, ["config", "user.email", "vibe-excel@local"])
        git(root, ["config", "commit.gpgSign", "false"])
        # 关闭换行转换，否则内容哈希会随平台漂移。
        git(root, ["config", "core.autocrlf", "false"])
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(
            "*.db\n*.db-wal\n*.db-shm\n*.xlsx\n*.xls\nout/\n__pycache__/\n", encoding="utf-8"
        )
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 程序仓库\n\n版本化的 Python、SQL、页面与业务规则。\n",
            encoding="utf-8",
        )
    commit_all(root, "init: program repository")


def head_sha(repo: str | Path, ref: str = "HEAD") -> str | None:
    """解析 ref 的提交哈希；ref 不存在（rev-parse 返回 1 且无输出）时返回 None。"""
    result = git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise VibeError(f"无法解析 {ref}: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(repo: str | Path) -> str:
    """返回当前分支名。"""
    return git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def branch_exists(repo: str | Path, branch: str) -> bool:
    """判断本地分支是否存在；返回码只在 0/1 有意义，其它视为错误。"""
    result = git(
        repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False
    )
    if result.returncode not in (0, 1):
        raise VibeError(f"无法检查分支 {branch}: {result.stderr.strip()}")
    return result.returncode == 0


def create_branch(repo: str | Path, branch: str, source: str) -> None:
    """从 source 创建分支（不切换）。"""
    git(repo, ["branch", branch, source])


def checkout(repo: str | Path, branch: str) -> None:
    """切换分支。"""
    git(repo, ["checkout", branch])


def is_clean(repo: str | Path) -> bool:
    """工作树是否没有未提交变更（含未跟踪文件）。"""
    return not git(repo, ["status", "--porcelain"]).stdout.strip()


def commit_all(repo: str | Path, message: str) -> str | None:
    """暂存全部变更并提交；工作树已干净时返回 None（不产生空提交）。"""
    if is_clean(repo):
        return None
    git(repo, ["add", "-A"])
    if COAUTHOR not in message:
        message = message.rstrip() + "\n\n" + COAUTHOR
    git(repo, ["commit", "-m", message])
    return head_sha(repo)


def commits_ahead(repo: str | Path, branch: str, base: str) -> int:
    """统计 branch 相对 base 领先的提交数。"""
    return int(git(repo, ["rev-list", "--count", f"{base}..{branch}"]).stdout.strip())


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    """判断 ancestor 是否为 descendant 的祖先；用于检测 main 被历史重写。"""
    result = git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    if result.returncode not in (0, 1):
        raise VibeError(f"无法比较 Git 历史: {result.stderr.strip()}")
    return result.returncode == 0


def merge_into(repo: str | Path, source: str, target: str) -> str:
    """把 source 以 --no-ff 合入 target，返回合并后的提交哈希。"""
    git(repo, ["checkout", target])
    # --no-ff 保留一条可见的发布合并记录，而不是快进。
    git(repo, ["merge", "--no-ff", "--no-edit", source, "-m", f"publish: {source} -> {target}\n\n{COAUTHOR}"])
    commit = head_sha(repo, target)
    if commit is None:
        raise VibeError("合并结束后没有目标提交")
    return commit


def add_worktree(repo: str | Path, worktree: str | Path, branch: str, source: str) -> None:
    """为分支加一个独立工作树；分支不存在时先从 source 创建。"""
    # 分支已存在时直接挂载，避免重复从 source 创建同名分支。
    if not branch_exists(repo, branch):
        create_branch(repo, branch, source)
    git(repo, ["worktree", "add", str(worktree), branch])


def remove_worktree(repo: str | Path, worktree: str | Path) -> None:
    """强制移除工作树目录；目录不存在时静默跳过。"""
    if Path(worktree).exists():
        git(repo, ["worktree", "remove", "--force", str(worktree)])


def read_file_at_commit(repo: str | Path, commit: str, path: str) -> str:
    """读取某提交里的文件内容；用于按提交解析流程而不碰工作区。"""
    # show 直接读提交里的 blob，不依赖工作区状态。
    result = git(repo, ["show", f"{commit}:{path}"], check=False)
    if result.returncode != 0:
        raise VibeError(
            f"无法读取 {path}@{commit[:8]}（请先提交该文件）: {result.stderr.strip()}"
        )
    return result.stdout
