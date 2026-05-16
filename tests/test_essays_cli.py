"""Тесты на sbc-validate-essays."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from validators.essays_cli import app

runner = CliRunner()


VALID_MDX = """---
id: zerkalo-memory-layers
title: Слои памяти в «Зеркале»
author: redaktor
films: [zerkalo-1974]
motifs: [rain-as-memory]
references: []
published: 2026-05-20
reading_time_min: 12
license: CC-BY-NC-SA-4.0
schema_version: 1
---

Текст разбора.
"""


def test_ok(tmp_path: Path) -> None:
    (tmp_path / "zerkalo-memory-layers.mdx").write_text(VALID_MDX, encoding="utf-8")
    result = runner.invoke(app, [str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_no_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "broken.mdx").write_text("Просто текст без frontmatter.\n", encoding="utf-8")
    result = runner.invoke(app, [str(tmp_path)])
    assert result.exit_code == 1
    assert "frontmatter" in result.output.lower()


def test_id_mismatch(tmp_path: Path) -> None:
    (tmp_path / "wrong-name.mdx").write_text(VALID_MDX, encoding="utf-8")
    result = runner.invoke(app, [str(tmp_path)])
    assert result.exit_code == 1
    assert "id" in result.output.lower()


def test_empty_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, [str(tmp_path)])
    assert result.exit_code == 0, result.output
