"""Обогащение фильмов постерами и фоновыми кадрами через TMDB API.

Дополняет enrich-films (который качает данные из Wikidata): TMDB богаче
покрытием постерами советских фильмов. Берёт `external_ids.tmdb` из
YAML, запрашивает /movie/<tmdb_id>, кладёт `poster_tmdb_path`.

Ключ API передаётся через переменную окружения TMDB_API_KEY (v3 API).
Зарегистрировать ключ: https://www.themoviedb.org/settings/api

Запуск:
    TMDB_API_KEY=... sbc-enrich-tmdb --out ../soviet-cinema-data --sleep 0.3
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import typer
from rich.console import Console
from rich.progress import Progress

from .util import dump_yaml, load_yaml_files

app = typer.Typer(add_completion=False)
console = Console()

TMDB_API_BASE = "https://api.themoviedb.org/3"
USER_AGENT = (
    "SovietBlocCinemaBot/0.1 "
    "(https://github.com/soviet-kino; cultivateweb@gmail.com)"
)


@dataclass
class Report:
    films_with_tmdb_id: int = 0
    films_updated: int = 0
    fields_filled: dict[str, int] = field(default_factory=dict)
    tmdb_requests: int = 0
    rate_limited: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)

    def bump(self, name: str) -> None:
        self.fields_filled[name] = self.fields_filled.get(name, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "films_with_tmdb_id": self.films_with_tmdb_id,
            "films_updated": self.films_updated,
            "fields_filled": dict(sorted(self.fields_filled.items())),
            "tmdb_requests": self.tmdb_requests,
            "rate_limited": self.rate_limited,
            "not_found": self.not_found,
            "errors": self.errors,
        }


def _fetch_movie(
    session: requests.Session, tmdb_id: str, api_key: str
) -> dict[str, Any] | None:
    """Тянет /movie/<tmdb_id>. Возвращает payload или None для 404.

    На 429 (rate-limit) — ждёт указанный в Retry-After интервал и
    повторяет до 5 раз. Иные сетевые ошибки кидают исключение, чтобы
    верхний уровень их залогировал и не маскировал.
    """
    url = f"{TMDB_API_BASE}/movie/{tmdb_id}"
    params = {"api_key": api_key, "language": "ru-RU"}
    for attempt in range(5):
        r = session.get(url, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "10"))
            console.print(f"[yellow]429, ждём {wait}с (попытка {attempt + 1})[/yellow]")
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(f"TMDB не отдала ответ за 5 попыток (tmdb_id={tmdb_id})")


@app.command()
def main(
    out: Path = typer.Option(..., "--out", help="Путь к корню soviet-cinema-data"),
    sleep: float = typer.Option(
        0.3,
        "--sleep",
        help="Пауза между запросами. TMDB допускает ~50 req/sec, "
        "но мы спокойны и не давим инфраструктуру.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Для теста — обработать первые N"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        console.print(
            "[red]TMDB_API_KEY не задан. Получить ключ: "
            "https://www.themoviedb.org/settings/api[/red]"
        )
        raise typer.Exit(code=1)

    out = out.resolve()
    films_dir = out / "films"
    reports_dir = out / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    films = load_yaml_files(films_dir)
    targets: list[tuple[str, str]] = []
    for slug, f in films.items():
        tmdb = (f.get("external_ids") or {}).get("tmdb")
        if not tmdb:
            continue
        targets.append((slug, str(tmdb)))
    if limit:
        targets = targets[:limit]

    report = Report(films_with_tmdb_id=len(targets))
    console.print(
        f"[bold]TMDB enrichment[/bold]: {len(targets)} фильмов с tmdb_id, "
        f"пауза {sleep}с"
    )

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    with Progress() as progress:
        task = progress.add_task("TMDB", total=len(targets))
        for slug, tmdb_id in targets:
            film = films.get(slug)
            if not film:
                progress.advance(task)
                continue

            try:
                payload = _fetch_movie(session, tmdb_id, api_key)
            except Exception as exc:
                report.errors.append(f"{slug}: {exc}")
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue
            report.tmdb_requests += 1

            if payload is None:
                report.not_found += 1
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue

            changed = False
            poster_path = payload.get("poster_path")
            if poster_path and not film.get("poster_tmdb_path"):
                # poster_path выглядит как "/abcXYZ.jpg" — храним как есть.
                film["poster_tmdb_path"] = poster_path
                report.bump("poster_tmdb_path")
                changed = True

            backdrop_path = payload.get("backdrop_path")
            if backdrop_path and not film.get("backdrop_tmdb_path"):
                film["backdrop_tmdb_path"] = backdrop_path
                report.bump("backdrop_tmdb_path")
                changed = True

            if changed:
                if not dry_run:
                    (films_dir / f"{slug}.yaml").write_text(
                        dump_yaml(film), encoding="utf-8"
                    )
                report.films_updated += 1

            progress.advance(task)
            if sleep:
                time.sleep(sleep)

    if not dry_run:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        report_path = reports_dir / f"enrich-tmdb-{ts}.yaml"
        report_path.write_text(dump_yaml(report.to_dict()), encoding="utf-8")
        console.print(
            f"[green]готово.[/green] обновлено {report.films_updated} фильмов, "
            f"отчёт: {report_path.relative_to(out)}"
        )
    else:
        console.print(report.to_dict())


if __name__ == "__main__":
    app()
