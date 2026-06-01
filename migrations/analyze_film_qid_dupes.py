"""Анализ дублей фильмов по wikidata QID (read-only).

Баг: импортёр создавал отдельный films/<slug>.yaml на каждую дату P577
одного фильма (разные годы проката) → несколько файлов с одним QID.

Скрипт НИЧЕГО не меняет. Печатает:
  - группы дублей (QID → файлы, годы),
  - какие из дублей обогащены (есть director/cast/...),
  - затронуты ли ссылки в references/ и collections/.

Запуск:  python3 migrations/analyze_film_qid_dupes.py <data_root>
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import yaml

# Поля, наличие которых означает «запись обогащена», а не сырая заглушка.
RICH_FIELDS = [
    "director", "screenwriter", "cinematographer", "composer", "studio",
    "cast", "topics", "genre", "poster_commons", "poster_tmdb_path",
    "censorship_status", "release_date", "title_en", "title_translit",
]


def richness(d: dict) -> int:
    return sum(1 for f in RICH_FIELDS if d.get(f) not in (None, "", [], {}))


def load(p: Path):
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"  ! ошибка чтения {p.name}: {e}")
        return {}


def main(root: Path) -> None:
    films_dir = root / "films"
    qid_to_files: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for p in sorted(films_dir.glob("*.yaml")):
        d = load(p)
        q = (d.get("external_ids") or {}).get("wikidata")
        if q:
            qid_to_files[q].append((p.stem, d))

    dupes = {q: fs for q, fs in qid_to_files.items() if len(fs) > 1}
    extra = sum(len(fs) - 1 for fs in dupes.values())
    print(f"QID-дублей: {len(dupes)} | лишних файлов: {extra}")

    # Сколько групп содержат хотя бы одну обогащённую запись
    groups_with_rich = 0
    multi_rich = 0  # группы где >1 обогащённой (нужен merge)
    diff_title = 0  # группы где title_ru/slug-база различаются
    for _q, fs in dupes.items():
        rich = [(s, d) for s, d in fs if richness(d) > 0]
        if rich:
            groups_with_rich += 1
        if len(rich) > 1:
            multi_rich += 1
        titles = {(_d.get("title_ru") or "") for _s, _d in fs}
        if len(titles) > 1:
            diff_title += 1
    print(f"групп с обогащённой записью: {groups_with_rich}")
    print(f"групп с >1 обогащённой (нужен merge данных): {multi_rich}")
    print(f"групп с разным title_ru внутри: {diff_title}")

    # Все slug, которые потенциально будут удалены (все кроме canonical).
    # canonical = max richness, tie → min year.
    all_dupe_slugs: set[str] = set()
    losers: set[str] = set()
    for _q, fs in dupes.items():
        def year_of(d):
            return d.get("year") or 9999
        winner = max(fs, key=lambda sd: (richness(sd[1]), -year_of(sd[1])))
        for s, _d in fs:
            all_dupe_slugs.add(s)
            if s != winner[0]:
                losers.add(s)

    # Проверка ссылок на удаляемые slug в references/ и collections/
    refs_hits = scan_refs(root / "references", losers)
    coll_hits = scan_collections(root / "collections", losers)
    print(f"\nудаляемых (loser) slug: {len(losers)}")
    print(f"ссылок на них в references/: {len(refs_hits)}")
    print(f"ссылок на них в collections/: {len(coll_hits)}")
    if refs_hits:
        print("  references:", refs_hits[:20])
    if coll_hits:
        print("  collections:", coll_hits[:20])

    print("\nпримеры групп (топ по числу файлов):")
    for q, fs in sorted(dupes.items(), key=lambda x: -len(x[1]))[:10]:
        rows = ", ".join(
            f"{s}(r{richness(d)},y{d.get('year')})" for s, d in fs
        )
        print(f"  {q}: {rows}")


def scan_refs(refs_dir: Path, losers: set[str]) -> list[str]:
    hits = []
    if not refs_dir.exists():
        return hits
    for p in refs_dir.glob("*.yaml"):
        d = load(p)
        sf = d.get("source_film")
        tgt = (d.get("target") or {})
        tref = tgt.get("ref") if tgt.get("type") == "film" else None
        if sf in losers or tref in losers:
            hits.append(p.stem)
    return hits


def scan_collections(coll_dir: Path, losers: set[str]) -> list[str]:
    hits = []
    if not coll_dir.exists():
        return hits
    for p in coll_dir.glob("*.yaml"):
        d = load(p)
        for key in ("films", "film_list", "items"):
            for item in (d.get(key) or []):
                ref = item if isinstance(item, str) else item.get("film")
                if ref in losers:
                    hits.append(f"{p.stem}:{ref}")
    return hits


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path("."))
