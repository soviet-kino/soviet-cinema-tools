"""Обогащение персон из Wikidata.

Для каждой персоны с external_ids.wikidata пакетным SPARQL тянем:
  - P569 birth date
  - P570 death date
  - P18 image (Commons)

Заполняются только пустые поля. Перезаписи нет.

Запуск:
    sbc-enrich-people --out ../soviet-cinema-data --chunk-size 100 --sleep 2
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
    load_yaml_files,
    parse_commons_filename,
    parse_qid,
)

app = typer.Typer(add_completion=False)
console = Console()

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = (
    "SovietBlocCinemaBot/0.1 "
    "(https://github.com/soviet-kino; cultivateweb@gmail.com)"
)


SPARQL_BATCH = """
SELECT ?person ?birth ?death ?image WHERE {{
  VALUES ?person {{ {qids} }}
  OPTIONAL {{ ?person wdt:P569 ?birth }}
  OPTIONAL {{ ?person wdt:P570 ?death }}
  OPTIONAL {{ ?person wdt:P18 ?image }}
}}
"""


@dataclass
class EnrichReport:
    people_with_qid: int = 0
    people_updated: int = 0
    fields_filled: dict[str, int] = field(default_factory=dict)
    sparql_chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def bump(self, name: str) -> None:
        self.fields_filled[name] = self.fields_filled.get(name, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "people_with_qid": self.people_with_qid,
            "people_updated": self.people_updated,
            "fields_filled": dict(sorted(self.fields_filled.items())),
            "sparql_chunks": self.sparql_chunks,
            "errors": self.errors,
        }


def _sparql_batch(
    qids: list[str], max_retries: int = 8, sleep_on_429: int = 65
) -> list[dict[str, Any]]:
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


def _to_partial_date(iso: str) -> str | None:
    """Wikidata отдаёт даты как 1932-04-04T00:00:00Z. Нам нужен YYYY-MM-DD.

    Wikidata также может вернуть «unknown value» как URL (например,
    `http://www.wikidata.org/.well-known/genid/...`). Поэтому строго
    требуем, чтобы строка начиналась с цифры. Даты до н.э. (с минусом)
    тоже отбрасываем — не наш период.
    """
    if not iso or not iso[:1].isdigit():
        return None
    return iso[:10] if len(iso) >= 10 else None


@app.command()
def main(
    out: Path = typer.Option(..., "--out", help="Путь к корню soviet-cinema-data"),
    chunk_size: int = typer.Option(100, "--chunk-size"),
    sleep: float = typer.Option(2.0, "--sleep"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    out = out.resolve()
    people_dir = out / "people"
    reports_dir = out / "reports"
    for d in (people_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    people = load_yaml_files(people_dir)
    qid_to_slug: dict[str, str] = {}
    for slug, p in people.items():
        qid = (p.get("external_ids") or {}).get("wikidata")
        if isinstance(qid, str) and qid.startswith("Q"):
            qid_to_slug[qid] = slug

    qids = list(qid_to_slug.keys())
    report = EnrichReport(people_with_qid=len(qids))
    console.print(f"[bold]Обогащение персон[/bold]: с QID — {len(qids)}, чанки по {chunk_size}")

    with Progress() as progress:
        task = progress.add_task("SPARQL", total=(len(qids) + chunk_size - 1) // chunk_size)
        for i in range(0, len(qids), chunk_size):
            chunk = qids[i : i + chunk_size]
            try:
                rows = _sparql_batch(chunk)
            except Exception as exc:
                report.errors.append(f"chunk {i // chunk_size}: {exc}")
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue
            report.sparql_chunks += 1

            # Объединяем результаты по persone (несколько строк могут быть
            # из-за нескольких значений P18).
            by_person: dict[str, dict[str, list[str]]] = {}
            for row in rows:
                person_qid = parse_qid(_value(row, "person") or "")
                if not person_qid:
                    continue
                bucket = by_person.setdefault(person_qid, {})
                for key in ("birth", "death", "image"):
                    val = _value(row, key)
                    if val:
                        bucket.setdefault(key, []).append(val)

            for person_qid, props in by_person.items():
                slug = qid_to_slug.get(person_qid)
                if not slug:
                    continue
                person = people.get(slug)
                if not person:
                    continue
                changed = False

                if not person.get("birth"):
                    for raw in props.get("birth", []):
                        d = _to_partial_date(raw)
                        if d:
                            person["birth"] = d
                            report.bump("birth")
                            changed = True
                            break
                if not person.get("death"):
                    for raw in props.get("death", []):
                        d = _to_partial_date(raw)
                        if d:
                            person["death"] = d
                            report.bump("death")
                            changed = True
                            break
                if not person.get("image_commons"):
                    for raw in props.get("image", []):
                        fname = parse_commons_filename(raw)
                        if fname:
                            person["image_commons"] = fname
                            report.bump("image_commons")
                            changed = True
                            break

                if changed and not dry_run:
                    (people_dir / f"{slug}.yaml").write_text(dump_yaml(person), encoding="utf-8")
                if changed:
                    report.people_updated += 1

            progress.advance(task)
            if sleep:
                time.sleep(sleep)

    if not dry_run:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        report_path = reports_dir / f"enrich-people-{ts}.yaml"
        report_path.write_text(dump_yaml(report.to_dict()), encoding="utf-8")
        console.print(f"[green]готово.[/green] отчёт: {report_path.relative_to(out)}")
    else:
        console.print(report.to_dict())


if __name__ == "__main__":
    app()
