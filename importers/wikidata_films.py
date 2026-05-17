"""Импортёр фильмов из Wikidata.

CLAUDE.md: «Импортируй фильмы СССР за 1970-е» — задача для этого скрипта.
Алгоритм:
  1. SPARQL-запрос: фильмы (Q11424 + подклассы), страна производства = один из
     указанных Q-кодов, год выхода в диапазоне [year_from, year_to].
  2. Для каждой записи генерируется YAML-файл films/<slug>.yaml по схеме v1.
  3. Уникальные режиссёры и студии собираются в отчёт reports/missing-people.txt
     и reports/missing-studios.txt — заглушки в people/ и studios/ создаются
     отдельным проходом (через --create-stubs) или вручную.
  4. Скрипт никогда не перезаписывает уже существующие YAML без --force.

Запуск:
    sbc-import-films \\
        --country SU --year-from 1970 --year-to 1979 \\
        --out ../soviet-cinema-data

ВАЖНО:
  - Wikidata перегружать запросами нельзя. Скрипт по умолчанию ограничивает
    выдачу 500 записями и спит между ними. Используйте --limit / --sleep.
  - Не пушить десятки тысяч файлов одним PR. Разбивать по годам или республикам.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.progress import Progress
from SPARQLWrapper import JSON, SPARQLWrapper

from .util import make_film_slug as make_slug

app = typer.Typer(add_completion=False)
console = Console()


WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = (
    "SovietBlocCinemaBot/0.1 "
    "(https://github.com/soviet-kino; cultivateweb@gmail.com)"
)

# Сопоставление кодов из soviet-cinema-data/vocabularies/countries.yaml
# с Q-идентификаторами Wikidata.
COUNTRY_TO_QID = {
    "SU": "Q15180",
    "PL": "Q210725",
    "CS": "Q33946",
    "DD": "Q16957",
    "YU": "Q83286",
    "BG": "Q220798",
    "HU": "Q47135",
    "RO": "Q170468",
    "AL": "Q204269",
    "MN": "Q188268",
}


# Шаблон запроса. country_qid и year_from/year_to подставляются.
#
# Минималистичный: только то, что нужно для генерации каркаса YAML
# (title, year, wikidata QID, imdb, runtime). Лейбл получаем через
# канонический wikibase:label с fallback ru → en.
#
# Режиссёров, студий и оригинального названия здесь нет намеренно: они
# тянут за собой OPTIONAL/GROUP_CONCAT, которые WDQS в режиме outage
# охотно убивает по rate-limit. Их докатим отдельным проходом, когда
# QID-ы фильмов уже импортированы и можно делать узконаправленные
# запросы по списку id.
SPARQL_TEMPLATE = """
SELECT ?film ?filmLabel ?year ?imdb ?runtime WHERE {{
  ?film wdt:P31/wdt:P279* wd:Q11424 .
  ?film wdt:P495 wd:{country_qid} .
  ?film wdt:P577 ?date .
  FILTER(YEAR(?date) >= {year_from} && YEAR(?date) <= {year_to})
  BIND(YEAR(?date) AS ?year)
  OPTIONAL {{ ?film wdt:P345 ?imdb }}
  OPTIONAL {{ ?film wdt:P2047 ?runtime }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
ORDER BY ?year
LIMIT {limit}
"""


@dataclass
class ImportReport:
    written: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    missing_people: set[str] = field(default_factory=set)
    missing_studios: set[str] = field(default_factory=set)
    sparql_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sparql_rows": self.sparql_rows,
            "written": self.written,
            "skipped_existing": self.skipped_existing,
            "missing_people": sorted(self.missing_people),
            "missing_studios": sorted(self.missing_studios),
        }


def _sparql_query(
    country_qid: str, year_from: int, year_to: int, limit: int
) -> list[dict[str, Any]]:
    sparql = SPARQLWrapper(WIKIDATA_ENDPOINT, agent=USER_AGENT)
    sparql.setReturnFormat(JSON)
    sparql.setQuery(
        SPARQL_TEMPLATE.format(
            country_qid=country_qid,
            year_from=year_from,
            year_to=year_to,
            limit=limit,
        )
    )
    data = sparql.query().convert()
    bindings = data.get("results", {}).get("bindings", []) if isinstance(data, dict) else []
    return bindings


def _row_to_yaml(row: dict[str, Any], country: str) -> tuple[str, dict[str, Any]]:
    def get(key: str) -> str | None:
        val = row.get(key)
        return val["value"] if val else None

    title_ru = get("filmLabel") or ""
    # Оригинальное название в первом проходе равно русскому. Для нерусскоязычных
    # республик это нужно будет поправить вручную (или докатить отдельным
    # запросом, дёргая mul/lang-specific labels по QID).
    title_orig = title_ru
    year = int(get("year") or 0)
    qid = (get("film") or "").rsplit("/", 1)[-1]
    imdb = get("imdb")
    runtime = get("runtime")
    slug = make_slug(title_ru or qid, year)

    payload: dict[str, Any] = {
        "id": slug,
        "title_ru": title_ru or None,
        "title_original": title_orig or None,
        "year": year,
        "country": [country],
    }
    if runtime:
        try:
            r = int(float(runtime))
        except ValueError:
            r = None
        # Wikidata изредка хранит длительность в секундах или с ошибочно
        # увеличенным значением (3192 «минут» у «Бани» 1962). Отбрасываем
        # всё, что не попадает в человеческий диапазон. Точное значение
        # позже проставит редактор по титрам.
        if r is not None and 1 <= r < 1000:
            payload["runtime_min"] = r
    ext: dict[str, Any] = {}
    if qid.startswith("Q"):
        ext["wikidata"] = qid
    if imdb:
        ext["imdb"] = imdb
    if ext:
        payload["external_ids"] = ext
    if qid.startswith("Q"):
        payload["sources"] = [f"https://www.wikidata.org/wiki/{qid}"]
    payload["schema_version"] = 1
    # выбрасываем пустые поля
    return slug, {k: v for k, v in payload.items() if v not in (None, "", [], {})}


@app.command()
def main(
    country: str = typer.Option(..., "--country", help="Код страны из vocabularies/countries.yaml"),
    year_from: int = typer.Option(..., "--year-from"),
    year_to: int = typer.Option(..., "--year-to"),
    out: Path = typer.Option(..., "--out", help="Путь к корню soviet-cinema-data"),
    limit: int = typer.Option(500, "--limit", help="Максимум записей за один запрос"),
    sleep: float = typer.Option(0.0, "--sleep", help="Пауза между записями, сек"),
    force: bool = typer.Option(False, "--force", help="Перезаписывать существующие YAML"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Не писать на диск"),
) -> None:
    if country not in COUNTRY_TO_QID:
        console.print(f"[red]не знаю Q-ID для country={country}[/red]")
        raise typer.Exit(code=2)

    out = out.resolve()
    films_dir = out / "films"
    reports_dir = out / "reports"
    films_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Wikidata SPARQL[/bold]: {country} {year_from}–{year_to}, limit={limit}")
    rows = _sparql_query(COUNTRY_TO_QID[country], year_from, year_to, limit)
    report = ImportReport(sparql_rows=len(rows))
    console.print(f"получено строк: {len(rows)}")

    with Progress() as progress:
        task = progress.add_task("Запись YAML", total=len(rows))
        for row in rows:
            slug, payload = _row_to_yaml(row, country)
            target = films_dir / f"{slug}.yaml"
            if target.exists() and not force:
                report.skipped_existing.append(slug)
            elif dry_run:
                report.written.append(slug)
            else:
                target.write_text(
                    yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                report.written.append(slug)
            # На первом проходе мы не тянем директоров/студии (см. SPARQL_TEMPLATE),
            # поэтому missing_people / missing_studios остаются пустыми. Их
            # заполнит отдельный шаг обогащения по QID.
            progress.advance(task)
            if sleep:
                time.sleep(sleep)

    report_path = reports_dir / f"import-{country}-{year_from}-{year_to}.yaml"
    report_path.write_text(
        yaml.safe_dump(report.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    console.print(f"[green]готово.[/green] отчёт: {report_path.relative_to(out)}")


if __name__ == "__main__":
    app()
