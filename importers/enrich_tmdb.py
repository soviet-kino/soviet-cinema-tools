"""Обогащение фильмов постерами и фоновыми кадрами через TMDB API.

Дополняет enrich-films (который качает данные из Wikidata): TMDB богаче
покрытием постерами советских фильмов.

Два пути на фильм:
  - есть external_ids.tmdb → запрос /movie/<tmdb_id>;
  - нет tmdb, но есть imdb → /find?external_source=imdb_id (отдаёт tmdb
    id + poster_path + backdrop_path в одном запросе; найденный tmdb_id
    сохраняется в external_ids).
Кладёт poster_tmdb_path и backdrop_tmdb_path.

Ключ API передаётся через переменную окружения TMDB_API_KEY. Поддержаны
оба формата: v3 (короткая hex-строка, ?api_key=) и v4 Read Access Token
(JWT eyJ..., Authorization: Bearer). Зарегистрировать:
https://www.themoviedb.org/settings/api

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
    params = {"language": "ru-RU"}
    if api_key:  # v3 ключ как query-параметр; для v4 (Bearer) — None
        params["api_key"] = api_key
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


def _find_by_imdb(
    session: requests.Session, imdb_id: str, api_key: str
) -> dict[str, Any] | None:
    """Ищет фильм в TMDB по imdb_id через /find.

    В базе почти ни у кого нет external_ids.tmdb, зато у ~12k фильмов есть
    imdb. /find?external_source=imdb_id отдаёт tmdb id, poster_path и
    backdrop_path в одном запросе — это и tmdb_id для записи, и постер
    сразу, без второго обращения к /movie.

    Возвращает первый элемент movie_results (dict с id/poster_path/
    backdrop_path) или None, если совпадений нет. Retry на 429 как в
    _fetch_movie.
    """
    url = f"{TMDB_API_BASE}/find/{imdb_id}"
    params = {"language": "ru-RU", "external_source": "imdb_id"}
    if api_key:  # v3 ключ как query-параметр; для v4 (Bearer) — None
        params["api_key"] = api_key
    for attempt in range(5):
        r = session.get(url, params=params, timeout=20)
        if r.status_code == 200:
            results = r.json().get("movie_results") or []
            return results[0] if results else None
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "10"))
            console.print(f"[yellow]429, ждём {wait}с (попытка {attempt + 1})[/yellow]")
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(f"TMDB /find не ответила за 5 попыток (imdb={imdb_id})")


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

    # TMDB поддерживает два формата ключа:
    #   - v3: короткая hex-строка, передаётся как ?api_key=...
    #   - v4: JWT Read Access Token (eyJ...), передаётся как Bearer-заголовок
    # Детектим по точке (JWT состоит из трёх частей через точку).
    is_bearer = "." in api_key
    # query_key уходит в params для v3; для v4 авторизация в session-заголовке.
    query_key = None if is_bearer else api_key

    out = out.resolve()
    films_dir = out / "films"
    reports_dir = out / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    films = load_yaml_files(films_dir)
    # Цель: фильмы с готовым tmdb_id ИЛИ с imdb (для них найдём tmdb
    # через /find). Пропускаем те, у кого уже есть и постер, и backdrop —
    # для них в TMDB ходить незачем.
    targets: list[tuple[str, str | None, str | None]] = []
    for slug, f in films.items():
        ext = f.get("external_ids") or {}
        tmdb = ext.get("tmdb")
        imdb = ext.get("imdb")
        if not tmdb and not imdb:
            continue
        if f.get("poster_tmdb_path") and f.get("backdrop_tmdb_path"):
            continue
        targets.append((slug, str(tmdb) if tmdb else None, str(imdb) if imdb else None))
    if limit:
        targets = targets[:limit]

    report = Report(films_with_tmdb_id=len(targets))
    console.print(
        f"[bold]TMDB enrichment[/bold]: {len(targets)} фильмов (tmdb или imdb), "
        f"пауза {sleep}с"
    )

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if is_bearer:
        session.headers["Authorization"] = f"Bearer {api_key}"

    with Progress() as progress:
        task = progress.add_task("TMDB", total=len(targets))
        for slug, tmdb_id, imdb_id in targets:
            film = films.get(slug)
            if not film:
                progress.advance(task)
                continue

            poster_path = backdrop_path = found_tmdb = None
            try:
                if tmdb_id:
                    payload = _fetch_movie(session, tmdb_id, query_key)
                    report.tmdb_requests += 1
                    if payload is not None:
                        poster_path = payload.get("poster_path")
                        backdrop_path = payload.get("backdrop_path")
                else:
                    # только imdb — /find отдаёт tmdb id + постер сразу
                    res = _find_by_imdb(session, imdb_id, query_key)
                    report.tmdb_requests += 1
                    if res is not None:
                        found_tmdb = res.get("id")
                        poster_path = res.get("poster_path")
                        backdrop_path = res.get("backdrop_path")
                        payload = res
                    else:
                        payload = None
            except Exception as exc:
                report.errors.append(f"{slug}: {exc}")
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue

            if payload is None:
                report.not_found += 1
                progress.advance(task)
                if sleep:
                    time.sleep(sleep)
                continue

            changed = False
            # tmdb_id, найденный через /find, сохраняем в external_ids
            if found_tmdb and not (film.get("external_ids") or {}).get("tmdb"):
                ext = dict(film.get("external_ids") or {})
                ext["tmdb"] = found_tmdb
                film["external_ids"] = ext
                report.bump("tmdb_id")
                changed = True

            if poster_path and not film.get("poster_tmdb_path"):
                # poster_path выглядит как "/abcXYZ.jpg" — храним как есть.
                film["poster_tmdb_path"] = poster_path
                report.bump("poster_tmdb_path")
                changed = True

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
