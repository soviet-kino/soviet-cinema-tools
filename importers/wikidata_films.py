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
  VALUES ?country {{ {country_qids} }}
  ?film wdt:P495 ?country .
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

# Wikidata часто маркирует фильмы соцэры современным государством,
# а не социалистическим (Польша=Q36 вместо ПНР=Q210725). Здесь —
# альтернативные QID для импорта; SPARQL берёт UNION.
ALTERNATIVE_COUNTRY_QIDS = {
    "PL": ["Q210725", "Q36"],         # ПНР + Польша
    "CS": ["Q33946", "Q12569"],       # ЧССР + Чехословакия общая
    "DD": ["Q16957"],                  # ГДР — Wikidata разделяет с ФРГ
    "YU": ["Q83286", "Q36704"],       # СФРЮ + Югославия общая
    "BG": ["Q220798", "Q219"],         # НРБ + Болгария
    "HU": ["Q47135", "Q28"],           # ВНР + Венгрия
    "RO": ["Q170468", "Q218"],         # СРР + Румыния
    "AL": ["Q204269", "Q222"],         # НСРА + Албания
    "MN": ["Q188268", "Q711"],         # МНР + Монголия
}


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
    country_qids: list[str],
    year_from: int,
    year_to: int,
    limit: int,
    max_retries: int = 10,
    sleep_on_429: int = 70,
) -> list[dict[str, Any]]:
    """SPARQL-запрос с retry на 429.

    country_qids — список QID-ов (объединение через VALUES в SPARQL).
    Для PL это будет [Q210725, Q36] — и ПНР, и Польша в целом.

    Wikidata периодически вводит «aggressive rate-limit 1 req/min» —
    тогда повторяем с задержкой ~70 секунд, чтобы влезть в окно.
    """
    qids_formatted = " ".join(f"wd:{q}" for q in country_qids)
    sparql = SPARQLWrapper(WIKIDATA_ENDPOINT, agent=USER_AGENT)
    sparql.setReturnFormat(JSON)
    sparql.setQuery(
        SPARQL_TEMPLATE.format(
            country_qids=qids_formatted,
            year_from=year_from,
            year_to=year_to,
            limit=limit,
        )
    )
    import time as _time

    for attempt in range(max_retries):
        try:
            data = sparql.query().convert()
            return (
                data.get("results", {}).get("bindings", [])
                if isinstance(data, dict)
                else []
            )
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate-limit" in msg.lower():
                print(f"429, ждём {sleep_on_429}с (попытка {attempt + 1}/{max_retries})")
                _time.sleep(sleep_on_429)
                continue
            raise
    raise RuntimeError(f"Wikidata не отдала ответ за {max_retries} попыток")


def _dedupe_rows_by_film(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна строка на фильм: у фильма бывает несколько дат P577 (повторные
    прокаты, релизы в разных странах), и каждая приходит отдельной строкой.

    Берём минимальный год — он ближе всего к году производства / первой
    премьеры. Без этого на каждый год создавался бы свой YAML с тем же QID
    (исторический баг, вычищенный migrations/dedupe_film_qids.py).
    """
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        film_uri = (row.get("film") or {}).get("value")
        if not film_uri:
            continue
        try:
            year = int((row.get("year") or {}).get("value") or 0)
        except ValueError:
            year = 0
        prev = best.get(film_uri)
        if prev is None:
            best[film_uri] = row
            continue
        try:
            prev_year = int((prev.get("year") or {}).get("value") or 0)
        except ValueError:
            prev_year = 0
        if year and (not prev_year or year < prev_year):
            best[film_uri] = row
    return list(best.values())


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
    qids = ALTERNATIVE_COUNTRY_QIDS.get(country, [COUNTRY_TO_QID[country]])
    raw_rows = _sparql_query(qids, year_from, year_to, limit)
    rows = _dedupe_rows_by_film(raw_rows)
    report = ImportReport(sparql_rows=len(raw_rows))
    console.print(
        f"получено строк: {len(raw_rows)} → уникальных фильмов: {len(rows)}"
    )

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
