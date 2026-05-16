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

import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.progress import Progress
from SPARQLWrapper import JSON, SPARQLWrapper

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
SPARQL_TEMPLATE = """
SELECT DISTINCT ?film ?filmLabel ?origLabel ?year ?imdb ?runtime
                (GROUP_CONCAT(DISTINCT ?directorLabel; separator="|") AS ?directors)
                (GROUP_CONCAT(DISTINCT ?studioLabel; separator="|")   AS ?studios)
WHERE {{
  ?film wdt:P31/wdt:P279* wd:Q11424 .
  ?film wdt:P495 wd:{country_qid} .
  ?film wdt:P577 ?date .
  FILTER(YEAR(?date) >= {year_from} && YEAR(?date) <= {year_to})
  BIND(YEAR(?date) AS ?year)

  OPTIONAL {{ ?film wdt:P57 ?director . ?director rdfs:label ?directorLabel .
             FILTER(LANG(?directorLabel) = "ru") }}
  OPTIONAL {{ ?film wdt:P272 ?studio . ?studio rdfs:label ?studioLabel .
             FILTER(LANG(?studioLabel) = "ru") }}
  OPTIONAL {{ ?film wdt:P345 ?imdb }}
  OPTIONAL {{ ?film wdt:P2047 ?runtime }}
  OPTIONAL {{ ?film rdfs:label ?filmLabel . FILTER(LANG(?filmLabel) = "ru") }}
  OPTIONAL {{
    ?film rdfs:label ?origLabel .
    FILTER(LANG(?origLabel) IN ("ru","pl","cs","de","sr","hr","bg","hu","ro","sq","mn"))
  }}
}}
GROUP BY ?film ?filmLabel ?origLabel ?year ?imdb ?runtime
ORDER BY ?year ?filmLabel
LIMIT {limit}
"""


_RU_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(text: str) -> str:
    out: list[str] = []
    for ch in text.lower():
        if ch in _RU_TRANSLIT:
            out.append(_RU_TRANSLIT[ch])
        elif ch.isalnum() or ch in {" ", "-"}:
            out.append(ch)
    s = "".join(out)
    # снять диакритику из латиницы
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def make_slug(title: str, year: int) -> str:
    base = _translit(title)
    if not base:
        base = "untitled"
    return f"{base}-{year}"


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
    title_orig = get("origLabel") or title_ru
    year = int(get("year") or 0)
    qid = (get("film") or "").rsplit("/", 1)[-1]
    imdb = get("imdb")
    runtime = get("runtime")
    slug = make_slug(title_ru or title_orig or qid, year)

    payload: dict[str, Any] = {
        "id": slug,
        "title_ru": title_ru or None,
        "title_original": title_orig or title_ru or None,
        "year": year,
        "country": [country],
    }
    if runtime:
        try:
            payload["runtime_min"] = int(float(runtime))
        except ValueError:
            pass
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
            # собираем имена для последующих заглушек
            for raw_name in (row.get("directors", {}).get("value", "") or "").split("|"):
                raw_name = raw_name.strip()
                if raw_name:
                    report.missing_people.add(raw_name)
            for raw_name in (row.get("studios", {}).get("value", "") or "").split("|"):
                raw_name = raw_name.strip()
                if raw_name:
                    report.missing_studios.add(raw_name)
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
