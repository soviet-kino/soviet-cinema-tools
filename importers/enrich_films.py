"""Обогащение фильмов из Wikidata по их QID.

Что делает:
  1. Сканирует films/*.yaml в soviet-cinema-data.
  2. Берёт все, у которых есть external_ids.wikidata.
  3. Для каждого чанка из --chunk-size QID'ов делает один SPARQL-запрос:
     - P57 director
     - P58 screenwriter
     - P344 director of photography
     - P86 composer
     - P272 production company (studio)
     - P136 genre (для будущего, в этом проходе игнорируется)
     - P3383 film poster (приоритетный источник постера)
     - P18 image (запасной, иногда содержит постер, иногда кадр)
     - P1651 YouTube video id
     - P345 IMDb id
  4. Для каждого упомянутого QID-человека и QID-студии:
     - Если уже существует в people/ или studios/ — используем существующий slug.
     - Иначе создаём заглушку YAML с минимальным набором полей.
  5. В YAML фильма добавляются только пустые поля. Уже заполненное не трогается —
     данные, проставленные человеком, считаются авторитетными.
  6. Отчёт пишется в reports/enrich-films-<timestamp>.yaml.

Запуск:
    sbc-enrich-films --out ../soviet-cinema-data --chunk-size 50 --sleep 2
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import Progress
from SPARQLWrapper import JSON, SPARQLWrapper

from .util import (
    dump_yaml,
    index_by_qid,
    load_yaml_files,
    make_person_slug,
    make_studio_slug,
    parse_commons_filename,
    parse_qid,
    unique_slug,
)

app = typer.Typer(add_completion=False)
console = Console()

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = (
    "SovietBlocCinemaBot/0.1 "
    "(https://github.com/soviet-kino; cultivateweb@gmail.com)"
)


# Что просим SPARQL для каждого чанка QID-ов.
# UNION удобен тем, что выдаёт одну строку на (фильм, property, значение)
# и не создаёт декартовый продукт по нескольким property.
SPARQL_BATCH = """
SELECT ?film ?prop ?value ?valueLabel WHERE {{
  VALUES ?film {{ {qids} }}
  {{ ?film wdt:P57   ?value . BIND("director"        AS ?prop) }} UNION
  {{ ?film wdt:P58   ?value . BIND("screenwriter"    AS ?prop) }} UNION
  {{ ?film wdt:P344  ?value . BIND("cinematographer" AS ?prop) }} UNION
  {{ ?film wdt:P86   ?value . BIND("composer"        AS ?prop) }} UNION
  {{ ?film wdt:P272  ?value . BIND("studio"          AS ?prop) }} UNION
  {{ ?film wdt:P161  ?value . BIND("cast"            AS ?prop) }} UNION
  {{ ?film wdt:P18   ?value . BIND("image"           AS ?prop) }} UNION
  {{ ?film wdt:P3383 ?value . BIND("poster"          AS ?prop) }} UNION
  {{ ?film wdt:P1651 ?value . BIND("youtube"         AS ?prop) }} UNION
  {{ ?film wdt:P345  ?value . BIND("imdb"            AS ?prop) }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
"""


@dataclass
class EnrichReport:
    films_with_qid: int = 0
    films_updated: int = 0
    fields_filled: dict[str, int] = field(default_factory=dict)
    new_people: list[str] = field(default_factory=list)
    new_studios: list[str] = field(default_factory=list)
    sparql_chunks: int = 0
    sparql_retries: int = 0
    errors: list[str] = field(default_factory=list)

    def bump(self, field_name: str) -> None:
        self.fields_filled[field_name] = self.fields_filled.get(field_name, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "films_with_qid": self.films_with_qid,
            "films_updated": self.films_updated,
            "fields_filled": dict(sorted(self.fields_filled.items())),
            "new_people": sorted(self.new_people),
            "new_studios": sorted(self.new_studios),
            "sparql_chunks": self.sparql_chunks,
            "sparql_retries": self.sparql_retries,
            "errors": self.errors,
        }


def _sparql_batch(
    qids: list[str], max_retries: int = 8, sleep_on_429: int = 65
) -> list[dict[str, Any]]:
    """Один SPARQL-запрос на чанк, с retry на 429."""
    formatted = " ".join(f"wd:{q}" for q in qids)
    query = SPARQL_BATCH.format(qids=formatted)
    sparql = SPARQLWrapper(WIKIDATA_ENDPOINT, agent=USER_AGENT)
    sparql.setReturnFormat(JSON)
    sparql.setQuery(query)
    for attempt in range(max_retries):
        try:
            res = sparql.query().convert()
            return res.get("results", {}).get("bindings", []) if isinstance(res, dict) else []
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate-limit" in msg.lower():
                console.print(f"[yellow]429, ждём {sleep_on_429}с (попытка {attempt + 1})[/yellow]")
                time.sleep(sleep_on_429)
                continue
            raise
    raise RuntimeError(f"Wikidata не отдала ответ за {max_retries} попыток")


def _value(row: dict[str, Any], key: str) -> str | None:
    v = row.get(key)
    return v.get("value") if v else None


def _resolve_or_create_person(
    qid: str,
    label: str | None,
    people_idx: dict[str, str],
    people: dict[str, dict[str, Any]],
    taken: set[str],
    role_hint: str,
    out_dir: Path,
    report: EnrichReport,
) -> str | None:
    """Возвращает slug персоны; создаёт заглушку если нужно."""
    if qid in people_idx:
        return people_idx[qid]
    if not label:
        # Без имени stub бесполезен — пропускаем, потом дозаполнит человек.
        return None
    base = make_person_slug(label)
    if not base:
        return None
    slug = unique_slug(base, taken)
    payload: dict[str, Any] = {
        "id": slug,
        "name_ru": label,
        "roles": [role_hint],
        "external_ids": {"wikidata": qid},
        "sources": [f"https://www.wikidata.org/wiki/{qid}"],
        "schema_version": 1,
    }
    (out_dir / f"{slug}.yaml").write_text(dump_yaml(payload), encoding="utf-8")
    people[slug] = payload
    people_idx[qid] = slug
    taken.add(slug)
    report.new_people.append(slug)
    return slug


def _resolve_or_create_studio(
    qid: str,
    label: str | None,
    studios_idx: dict[str, str],
    studios: dict[str, dict[str, Any]],
    taken: set[str],
    country_fallback: str,
    out_dir: Path,
    report: EnrichReport,
) -> str | None:
    if qid in studios_idx:
        return studios_idx[qid]
    if not label:
        return None
    base = make_studio_slug(label)
    if not base:
        return None
    slug = unique_slug(base, taken)
    payload: dict[str, Any] = {
        "id": slug,
        "name_ru": label,
        "country": country_fallback,
        "external_ids": {"wikidata": qid},
        "sources": [f"https://www.wikidata.org/wiki/{qid}"],
        "schema_version": 1,
    }
    (out_dir / f"{slug}.yaml").write_text(dump_yaml(payload), encoding="utf-8")
    studios[slug] = payload
    studios_idx[qid] = slug
    taken.add(slug)
    report.new_studios.append(slug)
    return slug


def _add_unique(target: dict[str, Any], key: str, value: str, report: EnrichReport) -> bool:
    """Добавить value в список target[key] если его там нет. Вернёт True если изменили."""
    existing = target.get(key) or []
    if value in existing:
        return False
    target[key] = [*existing, value]
    report.bump(key)
    return True


@app.command()
def main(
    out: Path = typer.Option(..., "--out", help="Путь к корню soviet-cinema-data"),
    chunk_size: int = typer.Option(40, "--chunk-size", help="Сколько QID в одном SPARQL-запросе"),
    sleep: float = typer.Option(2.0, "--sleep", help="Пауза между чанками, сек"),
    limit_films: int | None = typer.Option(
        None, "--limit-films", help="Обрабатывать только первые N (для отладки)"
    ),
    slugs: str | None = typer.Option(
        None,
        "--slugs",
        help="Список slug-ов фильмов через запятую (например 'zerkalo-1974,stalker-1979').",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Не писать на диск"),
) -> None:
    out = out.resolve()
    films_dir = out / "films"
    people_dir = out / "people"
    studios_dir = out / "studios"
    reports_dir = out / "reports"
    for d in (films_dir, people_dir, studios_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    films = load_yaml_files(films_dir)
    people = load_yaml_files(people_dir)
    studios = load_yaml_files(studios_dir)

    people_idx = index_by_qid(people)
    studios_idx = index_by_qid(studios)
    taken_people = set(people.keys())
    taken_studios = set(studios.keys())

    qid_to_slug = {(f.get("external_ids") or {}).get("wikidata"): slug for slug, f in films.items()}
    qid_to_slug.pop(None, None)

    film_qids = list(qid_to_slug.keys())
    if slugs:
        wanted = {s.strip() for s in slugs.split(",") if s.strip()}
        film_qids = [q for q, s in qid_to_slug.items() if s in wanted]
        missing = wanted - {qid_to_slug[q] for q in film_qids}
        if missing:
            console.print(f"[yellow]не найдены или без QID: {sorted(missing)}[/yellow]")
    if limit_films is not None:
        film_qids = film_qids[:limit_films]

    report = EnrichReport(films_with_qid=len(film_qids))
    console.print(
        f"[bold]Обогащение[/bold]: фильмов с QID — {len(film_qids)}, чанками по {chunk_size}"
    )

    with Progress() as progress:
        task = progress.add_task("SPARQL", total=(len(film_qids) + chunk_size - 1) // chunk_size)
        for i in range(0, len(film_qids), chunk_size):
            chunk = film_qids[i : i + chunk_size]
            try:
                rows = _sparql_batch(chunk)
            except Exception as exc:
                report.errors.append(f"chunk {i // chunk_size}: {exc}")
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue
            report.sparql_chunks += 1

            # Группируем строки по фильму и property.
            by_film: dict[str, dict[str, list[tuple[str | None, str | None]]]] = {}
            for row in rows:
                film_uri = _value(row, "film")
                prop = _value(row, "prop")
                value = _value(row, "value")
                label = _value(row, "valueLabel")
                film_qid = parse_qid(film_uri or "")
                if not film_qid or not prop:
                    continue
                by_film.setdefault(film_qid, {}).setdefault(prop, []).append((value, label))

            for film_qid, props in by_film.items():
                slug = qid_to_slug.get(film_qid)
                if not slug:
                    continue
                film = films.get(slug)
                if not film:
                    continue
                changed = False
                country_fallback = (film.get("country") or ["SU"])[0]

                # --- люди ---
                for sparql_prop, yaml_field, role_hint in [
                    ("director", "director", "director"),
                    ("screenwriter", "screenwriter", "screenwriter"),
                    ("cinematographer", "cinematographer", "cinematographer"),
                    ("composer", "composer", "composer"),
                ]:
                    for value_uri, label in props.get(sparql_prop, []):
                        person_qid = parse_qid(value_uri or "")
                        if not person_qid:
                            continue
                        person_slug = _resolve_or_create_person(
                            person_qid,
                            label,
                            people_idx,
                            people,
                            taken_people,
                            role_hint,
                            people_dir,
                            report,
                        )
                        if person_slug and _add_unique(film, yaml_field, person_slug, report):
                            changed = True

                # --- cast (актёры) ---
                # P161 без P453 (роли в кино) — приходит только список людей.
                # role в нашем YAML остаётся пустым, его заполняет редактор.
                existing_cast_slugs = {
                    entry.get("person")
                    for entry in (film.get("cast") or [])
                    if isinstance(entry, dict)
                }
                for value_uri, label in props.get("cast", []):
                    person_qid = parse_qid(value_uri or "")
                    if not person_qid:
                        continue
                    person_slug = _resolve_or_create_person(
                        person_qid,
                        label,
                        people_idx,
                        people,
                        taken_people,
                        "actor",
                        people_dir,
                        report,
                    )
                    if not person_slug:
                        continue
                    if person_slug in existing_cast_slugs:
                        continue
                    film.setdefault("cast", []).append({"person": person_slug})
                    existing_cast_slugs.add(person_slug)
                    report.bump("cast")
                    changed = True

                # --- студии ---
                for value_uri, label in props.get("studio", []):
                    studio_qid = parse_qid(value_uri or "")
                    if not studio_qid:
                        continue
                    studio_slug = _resolve_or_create_studio(
                        studio_qid,
                        label,
                        studios_idx,
                        studios,
                        taken_studios,
                        country_fallback,
                        studios_dir,
                        report,
                    )
                    if studio_slug and _add_unique(film, "studio", studio_slug, report):
                        changed = True

                # --- скаляры ---
                # poster_commons: предпочитаем P3383 (специально «постер»),
                # запасной вариант — P18 (общая картинка фильма, иногда
                # тоже постер, иногда кадр).
                if not film.get("poster_commons"):
                    for prop_name in ("poster", "image"):
                        for value_uri, _ in props.get(prop_name, []):
                            fname = parse_commons_filename(value_uri or "")
                            if fname:
                                film["poster_commons"] = fname
                                report.bump(f"poster_commons_via_{prop_name}")
                                changed = True
                                break
                        if film.get("poster_commons"):
                            break

                ext = film.setdefault("external_ids", {}) if "external_ids" in film else {}
                if "external_ids" not in film:
                    film["external_ids"] = ext
                if not ext.get("youtube"):
                    for value, _ in props.get("youtube", []):
                        if value:
                            ext["youtube"] = value
                            report.bump("external_ids.youtube")
                            changed = True
                            break
                if not ext.get("imdb"):
                    for value, _ in props.get("imdb", []):
                        if value:
                            ext["imdb"] = value
                            report.bump("external_ids.imdb")
                            changed = True
                            break
                # Если external_ids остался пуст — выпиливаем, чтобы не плодить шум.
                if not film["external_ids"]:
                    film.pop("external_ids", None)

                if changed and not dry_run:
                    (films_dir / f"{slug}.yaml").write_text(dump_yaml(film), encoding="utf-8")
                if changed:
                    report.films_updated += 1

            progress.advance(task)
            if sleep:
                time.sleep(sleep)

    if not dry_run:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        report_path = reports_dir / f"enrich-films-{ts}.yaml"
        report_path.write_text(dump_yaml(report.to_dict()), encoding="utf-8")
        console.print(f"[green]готово.[/green] отчёт: {report_path.relative_to(out)}")
    else:
        console.print("[yellow]dry-run, диск не трогали[/yellow]")
        console.print(report.to_dict())


if __name__ == "__main__":
    app()
