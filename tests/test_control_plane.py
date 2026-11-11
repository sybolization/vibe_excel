"""控制面回归：清单插值/补丁、插件宿主、CLI 入口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_excel.cli import CliOptions, parse_argv, run_cli
from vibe_excel.manifest import apply_patch, interpolate, parse_manifest
from vibe_excel.util import VibeError


def test_interpolate_expands_environment_and_default() -> None:
    value = interpolate(
        {"root": "${STORE:-.store}", "list": ["${NAME}"], "number": 3},
        {"NAME": "hr"},
    )
    assert value == {"root": ".store", "list": ["hr"], "number": 3}


def test_interpolate_requires_value_without_default() -> None:
    with pytest.raises(VibeError, match="没有值"):
        interpolate("${MISSING}", {})


def test_patch_replaces_whole_config_and_keeps_other_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "vibe.plugins.yml"
    manifest.write_text(
        "plugins:\n"
        "  - id: demo\n"
        "    name: demo_plugin\n"
        "    config: {keep: 1, drop: 2}\n",
        encoding="utf-8",
    )
    patch = tmp_path / "patch.yml"
    patch.write_text("- id: demo\n  config: {keep: 9}\n", encoding="utf-8")
    parsed = parse_manifest(manifest)
    patched = apply_patch(parsed, patch)
    assert patched.rows[0].config == {"keep": 9}
    assert patched.rows[0].name == "demo_plugin"


def test_patch_rejects_unknown_identifier(tmp_path: Path) -> None:
    manifest = tmp_path / "vibe.plugins.yml"
    manifest.write_text("plugins:\n  - id: demo\n    name: demo_plugin\n", encoding="utf-8")
    patch = tmp_path / "patch.yml"
    patch.write_text("- id: missing\n  disabled: true\n", encoding="utf-8")
    with pytest.raises(VibeError, match="找不到"):
        apply_patch(parse_manifest(manifest), patch)


def test_parse_argv_splits_command_and_flags() -> None:
    command, rest, flags = parse_argv(["run", "--as-of=2026-01-01", "extra", "--apply"])
    assert command == "run"
    assert rest == ["extra"]
    assert flags == {"as-of": "2026-01-01", "apply": "true"}


def _manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "vibe.plugins.yml"
    manifest.write_text(
        "plugins:\n"
        "  - id: commands\n"
        "    name: vibe_excel.plugins:commands\n",
        encoding="utf-8",
    )
    return manifest


def test_cli_lists_plugins_and_help(tmp_path: Path) -> None:
    lines: list[str] = []
    code = run_cli(
        ["plugins"],
        CliOptions(manifest=str(_manifest(tmp_path)), out=lines.append),
    )
    assert code == 0
    assert any("ACTIVE" in line and "commands" in line for line in lines)

    help_lines: list[str] = []
    code = run_cli(
        ["help"],
        CliOptions(manifest=str(_manifest(tmp_path)), out=help_lines.append),
    )
    assert code == 0
    assert help_lines[0].startswith("用法")
    assert any("host" not in line and "插件" in line for line in help_lines)


def test_cli_unknown_command_returns_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(
        ["nope"],
        CliOptions(manifest=str(_manifest(tmp_path)), out=lambda _line: None),
    )
    assert code == 2
    assert "未知命令" in capsys.readouterr().err
