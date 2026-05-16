"""Валидатор frontmatter MDX-эссе.

Запуск:
    sbc-validate-essays <директория>

Что делает:
  - находит все *.mdx в директории (рекурсивно);
  - вытаскивает YAML-frontmatter (блок между двумя строками `---`);
  - валидирует моделью Essay;
  - дополнительно проверяет, что имя файла соответствует id;
  - не лезет в смысловое содержание разбора (это задача редактора-человека).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from .schemas import Essay

app = typer.Typer(add_completion=False, help="Валидатор frontmatter эссе.")
console = Console()

# Frontmatter: ровно две строки `---` в начале файла, между ними YAML.
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


@dataclass
class Issue:
    path: Path
    message: str


def _extract_frontmatter(text: str) -> str | None:
    m = FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _validate_one(path: Path) -> list[Issue]:
    issues: list[Issue] = []
    text = path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(text)
    if fm is None:
        issues.append(Issue(path, "нет YAML-frontmatter (ожидался блок между `---`)"))
        return issues
    try:
        raw = yaml.safe_load(fm) or {}
    except yaml.YAMLError as exc:
        issues.append(Issue(path, f"невалидный YAML во frontmatter: {exc}"))
        return issues
    if not isinstance(raw, dict):
        issues.append(Issue(path, "frontmatter должен быть YAML-объектом"))
        return issues
    try:
        essay = Essay(**raw)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            issues.append(Issue(path, f"{loc}: {err['msg']}"))
        return issues
    if essay.id != path.stem:
        issues.append(
            Issue(path, f"id '{essay.id}' не совпадает с именем файла '{path.stem}'")
        )
    return issues


@app.command()
def main(
    essays_dir: Path = typer.Argument(..., help="Каталог с *.mdx"),
) -> None:
    essays_dir = essays_dir.resolve()
    if not essays_dir.is_dir():
        console.print(f"[red]не каталог:[/red] {essays_dir}")
        raise typer.Exit(code=2)

    mdx_files = sorted(essays_dir.rglob("*.mdx"))
    issues: list[Issue] = []
    for path in mdx_files:
        issues.extend(_validate_one(path))

    table = Table(title="Сводка")
    table.add_column("MDX-файлов")
    table.add_column("Ошибок", justify="right")
    table.add_row(str(len(mdx_files)), str(len(issues)))
    console.print(table)

    if issues:
        err_table = Table(title="Ошибки", show_lines=False)
        err_table.add_column("Файл")
        err_table.add_column("Сообщение")
        for it in issues:
            try:
                rel = it.path.relative_to(essays_dir)
            except ValueError:
                rel = it.path
            err_table.add_row(str(rel), it.message)
        console.print(err_table)
        raise typer.Exit(code=1)

    console.print("[green]frontmatter всех эссе валиден.[/green]")


if __name__ == "__main__":
    app()
