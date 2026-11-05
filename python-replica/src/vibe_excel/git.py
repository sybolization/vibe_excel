from __future__ import annotations

import subprocess
from pathlib import Path

from vibe_excel.util import VibeError

COAUTHOR = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


def git(
    repo: str | Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
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
        raise VibeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def init_repo(repo: str | Path, main_branch: str = "main") -> None:
    root = Path(repo)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, ["init", "-b", main_branch])
        git(root, ["config", "user.name", "Vibe Excel"])
        git(root, ["config", "user.email", "vibe-excel@local"])
        git(root, ["config", "commit.gpgSign", "false"])
        git(root, ["config", "core.autocrlf", "false"])
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(
            "*.db\n*.db-wal\n*.db-shm\n*.xlsx\n*.xls\nout/\n__pycache__/\n", encoding="utf-8"
        )
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Program repository\n\nVersioned Python, SQL, pages and business rules.\n",
            encoding="utf-8",
        )
    commit_all(root, "init: program repository")


def head_sha(repo: str | Path, ref: str = "HEAD") -> str | None:
    result = git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise VibeError(f"Cannot resolve {ref}: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(repo: str | Path) -> str:
    return git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def branch_exists(repo: str | Path, branch: str) -> bool:
    result = git(
        repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False
    )
    if result.returncode not in (0, 1):
        raise VibeError(f"Cannot inspect branch {branch}: {result.stderr.strip()}")
    return result.returncode == 0


def create_branch(repo: str | Path, branch: str, source: str) -> None:
    git(repo, ["branch", branch, source])


def checkout(repo: str | Path, branch: str) -> None:
    git(repo, ["checkout", branch])


def is_clean(repo: str | Path) -> bool:
    return not git(repo, ["status", "--porcelain"]).stdout.strip()


def commit_all(repo: str | Path, message: str) -> str | None:
    if is_clean(repo):
        return None
    git(repo, ["add", "-A"])
    if COAUTHOR not in message:
        message = message.rstrip() + "\n\n" + COAUTHOR
    git(repo, ["commit", "-m", message])
    return head_sha(repo)


def commits_ahead(repo: str | Path, branch: str, base: str) -> int:
    return int(git(repo, ["rev-list", "--count", f"{base}..{branch}"]).stdout.strip())


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    result = git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    if result.returncode not in (0, 1):
        raise VibeError(f"Cannot compare Git history: {result.stderr.strip()}")
    return result.returncode == 0


def merge_into(repo: str | Path, source: str, target: str) -> str:
    git(repo, ["checkout", target])
    git(repo, ["merge", "--no-ff", "--no-edit", source, "-m", f"publish: {source} -> {target}\n\n{COAUTHOR}"])
    commit = head_sha(repo, target)
    if commit is None:
        raise VibeError("Merge finished without a target commit")
    return commit


def add_worktree(repo: str | Path, worktree: str | Path, branch: str, source: str) -> None:
    if not branch_exists(repo, branch):
        create_branch(repo, branch, source)
    git(repo, ["worktree", "add", str(worktree), branch])


def remove_worktree(repo: str | Path, worktree: str | Path) -> None:
    if Path(worktree).exists():
        git(repo, ["worktree", "remove", "--force", str(worktree)])


def read_file_at_commit(repo: str | Path, commit: str, path: str) -> str:
    result = git(repo, ["show", f"{commit}:{path}"], check=False)
    if result.returncode != 0:
        raise VibeError(
            f"Cannot read {path}@{commit[:8]} (commit the file first): {result.stderr.strip()}"
        )
    return result.stdout
