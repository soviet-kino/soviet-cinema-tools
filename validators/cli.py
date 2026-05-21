"""CLI-валидатор данных soviet-cinema-data.

Что проверяет:
  1. Каждый YAML в films/, people/, studios/, motifs/, references/ соответствует
     своей Pydantic-модели.
  2. id из YAML совпадает с именем файла.
  3. Идентификаторы уникальны внутри каждой сущности.
  4. Перекрёстные ссылки разрешаются: director/cast/screenwriter и т.д. указывают
     на существующих people; studio — на существующие studios; source_film
     references — на существующий film.
  5. Значения полей-словарей (country, genre, role, motif category, reference kind,
     censorship_status, republic, language) присутствуют в соответствующем
     словаре в vocabularies/.

Запуск:
    python -m validators.cli <путь-к-soviet-cinema-data>

Выход 0 — всё ок, 1 — нашлись ошибки.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from .schemas import (
    ENTITY_MODELS,
    Collection,
    Film,
    Motif,
    Person,
    Reference,
    Studio,
    Topic,
)

app = typer.Typer(add_completion=False, help="Валидатор данных soviet-cinema-data.")
console = Console()


@dataclass
class Issue:
    path: Path
    message: str


@dataclass
class Loaded:
    films: dict[str, Film] = field(default_factory=dict)
    people: dict[str, Person] = field(default_factory=dict)
    studios: dict[str, Studio] = field(default_factory=dict)
    motifs: dict[str, Motif] = field(default_factory=dict)
    references: dict[str, Reference] = field(default_factory=dict)
    topics: dict[str, Topic] = field(default_factory=dict)
    collections: dict[str, Collection] = field(default_factory=dict)


@dataclass
class Vocab:
    countries: set[str] = field(default_factory=set)
    republics: set[str] = field(default_factory=set)
    genres: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    motif_categories: set[str] = field(default_factory=set)
    reference_kinds: set[str] = field(default_factory=set)
    censorship_statuses: set[str] = field(default_factory=set)
    languages: set[str] = field(default_factory=set)


def _load_vocab_codes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = raw.get("values", []) or []
    return {item["code"] for item in values if isinstance(item, dict) and "code" in item}


def _read_vocabularies(root: Path) -> Vocab:
    v = root / "vocabularies"
    return Vocab(
        countries=_load_vocab_codes(v / "countries.yaml"),
        republics=_load_vocab_codes(v / "republics.yaml"),
        genres=_load_vocab_codes(v / "genres.yaml"),
        roles=_load_vocab_codes(v / "roles.yaml"),
        motif_categories=_load_vocab_codes(v / "motif_categories.yaml"),
        reference_kinds=_load_vocab_codes(v / "reference_kinds.yaml"),
        censorship_statuses=_load_vocab_codes(v / "censorship_statuses.yaml"),
        languages=_load_vocab_codes(v / "languages.yaml"),
    )


def _iter_yaml(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix in {".yaml", ".yml"})


def _load_entities(root: Path) -> tuple[Loaded, list[Issue]]:
    loaded = Loaded()
    issues: list[Issue] = []
    for kind, model in ENTITY_MODELS.items():
        bucket: dict[str, Any] = getattr(loaded, kind)
        for path in _iter_yaml(root / kind):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                issues.append(Issue(path, f"невалидный YAML: {exc}"))
                continue
            if not isinstance(raw, dict):
                issues.append(Issue(path, "ожидался YAML-объект в корне"))
                continue
            try:
                obj = model(**raw)
            except ValidationError as exc:
                issues.append(Issue(path, f"схема: {_format_validation_error(exc)}"))
                continue
            expected_id = path.stem
            if obj.id != expected_id:
                issues.append(
                    Issue(path, f"id '{obj.id}' не совпадает с именем файла '{expected_id}'")
                )
                continue
            if obj.id in bucket:
                issues.append(Issue(path, f"дубль id '{obj.id}' в {kind}/"))
                continue
            bucket[obj.id] = obj
    return loaded, issues


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _check_missing_refs(
    path: Path,
    field_name: str,
    ids: list[str],
    bucket: dict[str, Any],
    issues: list[Issue],
) -> None:
    for ref in ids:
        if ref not in bucket:
            issues.append(Issue(path, f"{field_name}: '{ref}' не найден"))


def _check_film_refs(loaded: Loaded, vocab: Vocab, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, film in loaded.films.items():
        path = root / "films" / f"{slug}.yaml"
        _check_missing_refs(path, "director", film.director, loaded.people, issues)
        _check_missing_refs(path, "screenwriter", film.screenwriter, loaded.people, issues)
        _check_missing_refs(path, "cinematographer", film.cinematographer, loaded.people, issues)
        _check_missing_refs(path, "composer", film.composer, loaded.people, issues)
        _check_missing_refs(path, "studio", film.studio, loaded.studios, issues)
        _check_missing_refs(path, "topics", film.topics, loaded.topics, issues)
        for entry in film.cast:
            if entry.person not in loaded.people:
                issues.append(Issue(path, f"cast.person: '{entry.person}' не найден"))

        for code in film.country:
            if vocab.countries and code not in vocab.countries:
                issues.append(Issue(path, f"country: '{code}' нет в countries.yaml"))
        if film.republic and vocab.republics and film.republic not in vocab.republics:
            issues.append(Issue(path, f"republic: '{film.republic}' нет в republics.yaml"))
        for g in film.genre:
            if vocab.genres and g not in vocab.genres:
                issues.append(Issue(path, f"genre: '{g}' нет в genres.yaml"))
        for lng in film.language:
            if vocab.languages and lng not in vocab.languages:
                issues.append(Issue(path, f"language: '{lng}' нет в languages.yaml"))
        if (
            film.censorship_status
            and vocab.censorship_statuses
            and film.censorship_status not in vocab.censorship_statuses
        ):
            issues.append(
                Issue(
                    path,
                    f"censorship_status: '{film.censorship_status}' нет в словаре",
                )
            )
    return issues


def _check_person_refs(loaded: Loaded, vocab: Vocab, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, person in loaded.people.items():
        path = root / "people" / f"{slug}.yaml"
        for r in person.roles:
            if vocab.roles and r not in vocab.roles:
                issues.append(Issue(path, f"roles: '{r}' нет в roles.yaml"))
        for c in person.nationality:
            if vocab.countries and c not in vocab.countries:
                issues.append(Issue(path, f"nationality: '{c}' нет в countries.yaml"))
    return issues


def _check_studio_refs(loaded: Loaded, vocab: Vocab, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, studio in loaded.studios.items():
        path = root / "studios" / f"{slug}.yaml"
        if vocab.countries and studio.country not in vocab.countries:
            issues.append(Issue(path, f"country: '{studio.country}' нет в countries.yaml"))
    return issues


def _check_motif_refs(loaded: Loaded, vocab: Vocab, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, motif in loaded.motifs.items():
        path = root / "motifs" / f"{slug}.yaml"
        for cat in motif.category:
            if vocab.motif_categories and cat not in vocab.motif_categories:
                issues.append(Issue(path, f"category: '{cat}' нет в motif_categories.yaml"))
    return issues


def _check_topic_refs(loaded: Loaded, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, topic in loaded.topics.items():
        path = root / "topics" / f"{slug}.yaml"
        _check_missing_refs(path, "related_motifs", topic.related_motifs, loaded.motifs, issues)
    return issues


def _check_reference_refs(loaded: Loaded, vocab: Vocab, root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for slug, ref in loaded.references.items():
        path = root / "references" / f"{slug}.yaml"
        if ref.source_film not in loaded.films:
            issues.append(Issue(path, f"source_film: '{ref.source_film}' не найден"))
        if vocab.reference_kinds and ref.kind not in vocab.reference_kinds:
            issues.append(Issue(path, f"kind: '{ref.kind}' нет в reference_kinds.yaml"))
        target = ref.target
        if target.type == "film" and target.ref not in loaded.films:
            issues.append(Issue(path, f"target.ref: фильм '{target.ref}' не найден"))
        if target.type == "book":
            for a in target.authors:
                if a not in loaded.people:
                    issues.append(Issue(path, f"target.authors: '{a}' не найден"))
    return issues


@app.command()
def main(
    data_root: Path = typer.Argument(..., help="Путь к корню soviet-cinema-data"),
    strict_vocab: bool = typer.Option(
        True,
        "--strict-vocab/--no-strict-vocab",
        help="Падать на значениях, отсутствующих в словарях",
    ),
) -> None:
    data_root = data_root.resolve()
    if not data_root.is_dir():
        console.print(f"[red]не существует или не каталог:[/red] {data_root}")
        raise typer.Exit(code=2)

    vocab = _read_vocabularies(data_root)
    loaded, issues = _load_entities(data_root)

    # Перекрёстные проверки выполняем независимо от уже найденных ошибок —
    # так пользователь видит всю картину за один прогон.
    issues += _check_film_refs(loaded, vocab if strict_vocab else Vocab(), data_root)
    issues += _check_person_refs(loaded, vocab if strict_vocab else Vocab(), data_root)
    issues += _check_studio_refs(loaded, vocab if strict_vocab else Vocab(), data_root)
    issues += _check_motif_refs(loaded, vocab if strict_vocab else Vocab(), data_root)
    issues += _check_topic_refs(loaded, data_root)
    issues += _check_reference_refs(loaded, vocab if strict_vocab else Vocab(), data_root)

    _print_summary(loaded, issues, data_root)
    if issues:
        raise typer.Exit(code=1)


def _print_summary(loaded: Loaded, issues: list[Issue], root: Path) -> None:
    table = Table(title="Сводка")
    table.add_column("Сущность")
    table.add_column("Загружено", justify="right")
    for kind in ("films", "people", "studios", "motifs", "topics", "references"):
        table.add_row(kind, str(len(getattr(loaded, kind))))
    console.print(table)

    if not issues:
        console.print("[green]Все проверки пройдены.[/green]")
        return

    err_table = Table(title=f"Ошибки ({len(issues)})", show_lines=False)
    err_table.add_column("Файл")
    err_table.add_column("Сообщение")
    for it in issues:
        try:
            rel = it.path.relative_to(root)
        except ValueError:
            rel = it.path
        err_table.add_row(str(rel), it.message)
    console.print(err_table)


if __name__ == "__main__":
    app()
