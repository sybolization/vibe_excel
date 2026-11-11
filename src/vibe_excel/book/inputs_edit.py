"""构建期输入的编辑：加文件、移出、撤回，以及归档与事件。

页面上的「导入 / 删除 / 撤回 / 重建批次」最终都落到这里。三条约定：

1. **一次操作只做一件事**：先落文件、再改清单；中途失败就把已经做掉的回滚，
   不留「清单里有、目录里没有」这种半成品——那正是 ``setup`` 会直接报错的状态。
2. **没有真删**：文件先进 ``<store>/.trash/<时间戳>/``（撤回用），重建批次成功后再
   进 ``<store>/.archive/<时间戳>/`` 长期保留。agent 改脚本时要能读回原始文件。
3. **每次移出留一份「包」**（``packet.json``）：文件元信息、它物化出的表、受影响的
   链路、移出理由。将来接上对话传输时，agent 只需要读这个包。

清单只做「名单那几行」的定点改写，不重排整份 YAML，也不碰注释。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..util import VibeError

__all__ = [
    "ALLOWED_SUFFIXES",
    "GroundTruth",
    "add_file",
    "last_removal",
    "list_files",
    "promote_trash",
    "remove_file",
    "resolve_ground_truth",
    "undo_removal",
]

ALLOWED_SUFFIXES = (".xlsx", ".xls")
EVENTS_NAME = "input-events.jsonl"
TRASH_NAME = ".trash"
ARCHIVE_NAME = ".archive"
PACKET_NAME = "packet.json"

_VALUE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")


@dataclass(frozen=True)
class GroundTruth:
    """清单里 ``inputs`` 那一段解析出来的结果。"""

    manifest: Path
    directory: Path
    files: list[str]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _stamp() -> str:
    """归档目录名：本地时间到秒，够区分两次操作。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _expand(value: object) -> str:
    """展开 ``${VAR}`` / ``${VAR:-默认值}``；不做多层嵌套。"""
    text = "" if value is None else str(value)
    match = _VALUE.match(text.strip())
    if match is None:
        return text
    name, fallback = match.group(1), match.group(2)
    return os.environ.get(name) or (fallback or "")


def _sessions_config(text: str, where: Path) -> dict[str, object]:
    """从清单文本里取出 sessions 插件的 config。"""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise VibeError(f"清单不是合法 YAML：{where}：{error}") from error
    rows = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise VibeError(f"清单缺少 plugins 列表：{where}")
    for row in rows:
        if isinstance(row, dict) and row.get("id") == "sessions":
            config = row.get("config")
            if isinstance(config, dict):
                return config
    raise VibeError(f"清单里没有 sessions 插件的 config：{where}")


def resolve_ground_truth(manifest: str | Path | None = None) -> GroundTruth:
    """定位清单 → 解析 inputs 段 → 返回可编辑的 ground-truth 视图。"""
    if manifest is None:
        # 与 run_cli 保持同一个优先级：显式路径 → 环境变量 → 默认位置。
        # 否则页面改的清单和 setup 读的清单可能不是同一份。
        override = os.environ.get("VIBE_EXCEL_MANIFEST")
        if override:
            path = Path(override)
        else:
            from ..cli import default_manifest  # 只在需要时导入，避免循环依赖

            path = Path(default_manifest())
    else:
        path = Path(manifest)
    if not path.is_file():
        raise VibeError(f"找不到清单：{path}")
    text = path.read_text(encoding="utf-8")
    config = _sessions_config(text, path)
    inputs = config.get("inputs")
    if not isinstance(inputs, dict):
        raise VibeError(f"清单里没有 sessions.inputs：{path}")
    # 注意：空字符串会被 Path() 折成 "."，所以要先看原始文本再构造路径。
    raw_dir = _expand(inputs.get("dir")).strip()
    if not raw_dir:
        raise VibeError(
            f"清单里 sessions.inputs.dir 为空：{path}\n"
            "先设置环境变量（例如 VIBE_EXCEL_GROUND_TRUTH）再启动服务。"
        )
    directory = Path(raw_dir).expanduser().resolve()
    raw_files = inputs.get("files")
    names = [str(item) for item in raw_files] if isinstance(raw_files, list) else []
    return GroundTruth(path, directory, names)


def list_files(ground_truth: GroundTruth) -> list[dict[str, object]]:
    """列出目录里真实存在的输入文件；清单里有但文件没了的会带 missing 标记。"""
    rows: list[dict[str, object]] = []
    for name in ground_truth.files:
        target = ground_truth.directory / name
        rows.append(
            {
                "name": name,
                "bytes": target.stat().st_size if target.is_file() else 0,
                "listed": True,
                "missing": not target.is_file(),
            }
        )
    if ground_truth.directory.is_dir():
        known = set(ground_truth.files)
        for entry in sorted(ground_truth.directory.iterdir(), key=lambda item: item.name):
            if not entry.is_file() or entry.name in known:
                continue
            if entry.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            rows.append(
                {"name": entry.name, "bytes": entry.stat().st_size, "listed": False, "missing": False}
            )
    return rows


def _set_files(text: str, names: list[str]) -> str:
    """只改 ``inputs.files`` 下面那几行 ``- name``，其余原样保留。"""
    lines = text.splitlines(keepends=True)
    inputs_at = next(
        (index for index, line in enumerate(lines) if re.match(r"^\s+inputs:\s*$", line)),
        None,
    )
    if inputs_at is None:
        raise VibeError("清单里找不到 inputs: 段，无法写入名单")
    files_at = None
    for index in range(inputs_at + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= len(lines[inputs_at]) - len(lines[inputs_at].lstrip()):
            break
        if re.match(r"^\s+files:\s*$", line):
            files_at = index
            break
    if files_at is None:
        raise VibeError("清单 inputs 段里找不到 files:，无法写入名单")
    files_indent = len(lines[files_at]) - len(lines[files_at].lstrip())
    item_indent = " " * (files_indent + 2)
    end = files_at + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            end += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= files_indent or not line.lstrip().startswith("- "):
            break
        end += 1
    body = "".join(f"{item_indent}- {name}\n" for name in names)
    return "".join(lines[: files_at + 1]) + body + "".join(lines[end:])


def _write_files(ground_truth: GroundTruth, names: list[str]) -> None:
    text = ground_truth.manifest.read_text(encoding="utf-8")
    updated = _set_files(text, names)
    temporary = ground_truth.manifest.with_suffix(ground_truth.manifest.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temporary, ground_truth.manifest)


def _append_event(store: Path, event: dict[str, object]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    with (store / EVENTS_NAME).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def events(store: Path) -> list[dict[str, object]]:
    """读事件流水；坏行直接跳过，不让一条脏数据卡死整页。"""
    path = store / EVENTS_NAME
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def add_file(ground_truth: GroundTruth, name: str, data: bytes) -> dict[str, object]:
    """写文件并把名字登记进清单。

    先落文件再改清单：反过来的话，中途失败会留下「清单里有、目录里没有」，
    下一次 setup 直接报缺文件。
    """
    clean = Path(name).name
    if not clean or clean != name:
        raise VibeError(f"文件名不合法：{name}")
    if Path(clean).suffix.lower() not in ALLOWED_SUFFIXES:
        raise VibeError(f"只接受 {', '.join(ALLOWED_SUFFIXES)}：{clean}")
    if not data:
        raise VibeError(f"文件是空的：{clean}")
    ground_truth.directory.mkdir(parents=True, exist_ok=True)
    target = ground_truth.directory / clean
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    names = list(dict.fromkeys([*ground_truth.files, clean]))
    try:
        _write_files(ground_truth, names)
    except OSError:
        # 清单写不进去就把刚落下的文件撤掉，别留半成品
        target.unlink(missing_ok=True)
        raise
    return {
        "name": clean,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "replaced": clean in ground_truth.files,
    }


def remove_file(
    ground_truth: GroundTruth,
    store: Path,
    name: str,
    *,
    reason: str = "",
    tables: list[dict[str, object]] | None = None,
    chain: dict[str, object] | None = None,
) -> dict[str, object]:
    """把文件移出输入：挪进 .trash/、写包、记事件、从清单摘掉。"""
    clean = Path(name).name
    source = ground_truth.directory / clean
    if clean not in ground_truth.files and not source.is_file():
        raise VibeError(f"清单和目录里都没有这个文件：{clean}")
    stamp = _stamp()
    bucket = store / TRASH_NAME / stamp
    bucket.mkdir(parents=True, exist_ok=True)
    moved = False
    if source.is_file():
        shutil.move(str(source), str(bucket / clean))
        moved = True
    payload = {
        "schema": "vibe.inputs/1",
        "kind": "remove",
        "at": _now(),
        "file": clean,
        "bytes": (bucket / clean).stat().st_size if moved else 0,
        "sha256": _sha256(bucket / clean) if moved else "",
        "reason": reason,
        "manifest": str(ground_truth.manifest),
        "groundTruth": str(ground_truth.directory),
        "archivedAt": str(bucket),
        "tables": tables or [],
        "chain": chain or {},
    }
    (bucket / PACKET_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    names = [item for item in ground_truth.files if item != clean]
    try:
        _write_files(ground_truth, names)
    except OSError:
        # 清单没改成，就把文件挪回去，保持两边一致
        if moved:
            shutil.move(str(bucket / clean), str(source))
        raise
    event = {
        "kind": "remove",
        "at": payload["at"],
        "file": clean,
        "reason": reason,
        "trash": str(bucket),
        "packet": str(bucket / PACKET_NAME),
        "manifest": str(ground_truth.manifest),
    }
    _append_event(store, event)
    return {**event, "bytes": payload["bytes"], "sha256": payload["sha256"]}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def last_removal(store: Path) -> dict[str, object] | None:
    """最近一次还能撤回的移出操作（未被 undo 的那条）。"""
    undone: set[str] = set()
    pending: list[dict[str, object]] = []
    for item in events(store):
        if item.get("kind") == "remove":
            pending.append(item)
        elif item.get("kind") == "undo":
            undone.add(str(item.get("trash") or ""))
    for item in reversed(pending):
        if str(item.get("trash") or "") not in undone:
            return item
    return None


def undo_removal(ground_truth: GroundTruth, store: Path) -> dict[str, object]:
    """撤掉最近一次移出：文件挪回、名单补回、记一条 undo。"""
    target = last_removal(store)
    if target is None:
        raise VibeError("没有可撤回的移出操作")
    bucket = Path(str(target["trash"]))
    name = str(target["file"])
    archived = bucket / name
    if not archived.is_file():
        raise VibeError(f"归档里找不到文件，无法撤回：{archived}")
    ground_truth.directory.mkdir(parents=True, exist_ok=True)
    restored = ground_truth.directory / name
    if restored.exists():
        raise VibeError(f"目录里已经有同名文件，先处理它再撤回：{restored}")
    shutil.move(str(archived), str(restored))
    names = list(dict.fromkeys([*ground_truth.files, name]))
    try:
        _write_files(ground_truth, names)
    except OSError:
        shutil.move(str(restored), str(archived))
        raise
    event: dict[str, object] = {"kind": "undo", "at": _now(), "file": name, "trash": str(bucket)}
    _append_event(store, event)
    return event


def promote_trash(store: Path) -> list[str]:
    """重建批次成功后调用：把 .trash 归档成 .archive。

    这一步之前是「可以撤回」，之后是「历史输入」——两者的生命周期不同。
    """
    trash = store / TRASH_NAME
    if not trash.is_dir():
        return []
    archive = store / ARCHIVE_NAME
    # 撤回过的移出不算数：文件已经回到输入里，不该再留一份「已归档」的记录。
    undone = {str(item.get("trash") or "") for item in events(store) if item.get("kind") == "undo"}
    moved: list[str] = []
    for bucket in sorted(trash.iterdir()):
        if not bucket.is_dir():
            continue
        if str(bucket) in undone:
            shutil.rmtree(bucket, ignore_errors=True)
            continue
        # 真的搬东西时才建归档目录：什么都没归档就不该留下一个空壳
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / bucket.name
        if destination.exists():
            destination = archive / f"{bucket.name}-{len(moved) + 1}"
        shutil.move(str(bucket), str(destination))
        packet = destination / PACKET_NAME
        if packet.is_file():
            payload = json.loads(packet.read_text(encoding="utf-8"))
            payload["archivedAt"] = str(destination)
            packet.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        moved.append(str(destination))
    try:
        trash.rmdir()
    except OSError:
        pass
    return moved
