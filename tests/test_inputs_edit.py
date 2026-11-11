"""构建期输入的编辑：导入、移出、撤回、归档。

这里盯的是「不会留下半成品」：清单和目录必须始终一致，
否则下一次 setup 会直接报缺文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe_excel.book.inputs_edit import (
    PACKET_NAME,
    add_file,
    last_removal,
    list_files,
    promote_trash,
    remove_file,
    resolve_ground_truth,
    undo_removal,
)
from vibe_excel.util import VibeError

MANIFEST = """# 顶层注释要留住
plugins:
  - id: store
    name: vibe_excel.plugins:store
    config:
      storeRoot: '${VIBE_EXCEL_STORE:-.store}'
  - id: sessions
    name: vibe_excel.plugins:sessions
    config:
      program: demo
      dbFile: hr.db
      inputs:
        dir: '${VIBE_EXCEL_GROUND_TRUTH:-}'
        files:
          - orders.xlsx
"""


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """造一份清单 + 输入目录，返回 (ground-truth 目录, store 目录)。"""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "orders.xlsx").write_bytes(b"old")
    manifest = tmp_path / "vibe.plugins.yml"
    manifest.write_text(MANIFEST, encoding="utf-8")
    monkeypatch.setenv("VIBE_EXCEL_GROUND_TRUTH", str(inputs))
    store = tmp_path / "store"
    store.mkdir()
    return inputs, store


def test_resolve_reads_dir_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, _ = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")

    assert ground.directory == inputs
    assert ground.files == ["orders.xlsx"]


def test_resolve_without_dir_env_points_at_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case(tmp_path, monkeypatch)
    monkeypatch.delenv("VIBE_EXCEL_GROUND_TRUTH")
    with pytest.raises(VibeError, match="VIBE_EXCEL_GROUND_TRUTH"):
        resolve_ground_truth(tmp_path / "vibe.plugins.yml")


def test_list_files_marks_listed_and_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _ = _case(tmp_path, monkeypatch)
    (inputs / "extra.xlsx").write_bytes(b"x")
    (inputs / "notes.txt").write_bytes(b"x")
    rows = {row["name"]: row for row in list_files(resolve_ground_truth(tmp_path / "vibe.plugins.yml"))}

    assert rows["orders.xlsx"]["listed"] is True
    assert rows["orders.xlsx"]["missing"] is False
    # 目录里有、清单里没登记的 xlsx 也要露出来，否则用户看不见自己拖进来的东西
    assert rows["extra.xlsx"]["listed"] is False
    # 非输入格式不混进来
    assert "notes.txt" not in rows


def test_add_file_writes_and_registers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, _ = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")

    result = add_file(ground, "new.xlsx", b"payload")

    assert (inputs / "new.xlsx").read_bytes() == b"payload"
    assert result["bytes"] == 7
    text = (tmp_path / "vibe.plugins.yml").read_text(encoding="utf-8")
    assert "          - orders.xlsx\n          - new.xlsx\n" in text
    # 注释和别的插件不能被重排掉
    assert text.startswith("# 顶层注释要留住\n")
    assert "storeRoot" in text


def test_add_file_keeps_original_order_on_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")

    result = add_file(ground, "orders.xlsx", b"new")

    assert result["replaced"] is True
    text = (tmp_path / "vibe.plugins.yml").read_text(encoding="utf-8")
    assert text.count("- orders.xlsx") == 1


@pytest.mark.parametrize("name", ["note.txt", "", "../evil.xlsx", "sub/dir.xlsx"])
def test_add_file_rejects_bad_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    with pytest.raises(VibeError):
        add_file(ground, name, b"x")


def test_add_file_rejects_empty_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    with pytest.raises(VibeError, match="空的"):
        add_file(ground, "empty.xlsx", b"")


def test_remove_moves_to_trash_writes_packet_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, store = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")

    event = remove_file(
        ground,
        store,
        "orders.xlsx",
        reason="导出表换了结构",
        tables=[{"sheet": "Orders", "table": "raw_orders"}],
    )

    assert not (inputs / "orders.xlsx").exists()
    bucket = Path(str(event["trash"]))
    assert (bucket / "orders.xlsx").is_file()
    packet = json.loads((bucket / PACKET_NAME).read_text(encoding="utf-8"))
    assert packet["reason"] == "导出表换了结构"
    assert packet["tables"] == [{"sheet": "Orders", "table": "raw_orders"}]
    # 清单里也要摘掉
    assert "orders.xlsx" not in (tmp_path / "vibe.plugins.yml").read_text(encoding="utf-8")
    assert (store / "input-events.jsonl").is_file()


def test_remove_then_undo_restores_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, store = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    remove_file(ground, store, "orders.xlsx")

    assert last_removal(store) is not None
    undo_removal(ground, store)

    assert (inputs / "orders.xlsx").read_bytes() == b"old"
    assert "orders.xlsx" in (tmp_path / "vibe.plugins.yml").read_text(encoding="utf-8")
    # 撤过一次之后就没有可撤的了
    assert last_removal(store) is None
    with pytest.raises(VibeError, match="没有可撤回"):
        undo_removal(ground, store)


def test_undo_refuses_when_name_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs, store = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    remove_file(ground, store, "orders.xlsx")
    (inputs / "orders.xlsx").write_bytes(b"someone else put it back")

    with pytest.raises(VibeError, match="同名文件"):
        undo_removal(ground, store)


def test_promote_trash_moves_packets_to_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    remove_file(ground, store, "orders.xlsx", reason="换了一份导出")

    moved = promote_trash(store)

    assert len(moved) == 1
    packet = json.loads((Path(moved[0]) / PACKET_NAME).read_text(encoding="utf-8"))
    assert packet["archivedAt"] == moved[0]
    assert not (store / ".trash").exists()


def test_remove_missing_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store = _case(tmp_path, monkeypatch)
    ground = resolve_ground_truth(tmp_path / "vibe.plugins.yml")
    with pytest.raises(VibeError, match="都没有这个文件"):
        remove_file(ground, store, "nope.xlsx")
