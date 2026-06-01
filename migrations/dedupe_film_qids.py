"""Дедупликация фильмов по wikidata QID.

Баг импортёра: на каждую дату P577 (год проката) одного фильма создавался
отдельный films/<slug>.yaml с тем же QID. Часто обогащение enrich-films
попадало в файл с поздним (неверным) годом, а не с годом производства.

Стратегия дедупа на группу с одинаковым QID:
  - canonical_year = МИНИМАЛЬНЫЙ год среди файлов группы (≈ год
    производства / первой премьеры — верный год фильма);
  - объединяем поля: основа — файл с min-год (верные year/title/sources),
    обогащённые поля (director/cast/topics/poster/…) доливаем из самого
    заполненного файла группы;
  - canonical_slug = make_film_slug(title_ru, canonical_year);
  - остальные файлы группы удаляем;
  - ссылки на удалённые slug в references/ и collections/ переназначаем
    на canonical_slug.

По умолчанию DRY-RUN (только отчёт). Запись и удаление — с флагом --apply.

Запуск:
    python3 migrations/dedupe_film_qids.py <data_root>           # dry-run
    python3 migrations/dedupe_film_qids.py <data_root> --apply    # применить
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from importers.util import make_film_slug  # noqa: E402

RICH_FIELDS = [
    "director", "screenwriter", "cinematographer", "composer", "studio",
    "cast", "topics", "genre", "poster_commons", "poster_tmdb_path",
    "censorship_status", "release_date", "title_en", "title_translit",
]

# Канонический порядок полей для аккуратного YAML.
FIELD_ORDER = [
    "id", "title_ru", "title_original", "title_translit", "title_en",
    "year", "country", "republic", "studio", "director", "screenwriter",
    "cinematographer", "composer", "cast", "runtime_min", "language",
    "genre", "color", "release_date", "production_status",
    "censorship_status", "topics", "poster_commons", "poster_tmdb_path",
    "external_ids", "sources", "schema_version",
]


def richness(d: dict) -> int:
    return sum(1 for f in RICH_FIELDS if d.get(f) not in (None, "", [], {}))


def load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def order_fields(d: dict) -> dict:
    out = {k: d[k] for k in FIELD_ORDER if k in d and d[k] not in (None, "", [], {})}
    for k, v in d.items():  # хвост: незнакомые поля
        if k not in out and v not in (None, "", [], {}):
            out[k] = v
    return out


def merge_group(files: list[tuple[str, dict]]) -> tuple[str, dict]:
    """Возвращает (canonical_slug, merged_payload) для группы одного QID."""
    years = [d["year"] for _s, d in files if isinstance(d.get("year"), int)]
    canonical_year = min(years) if years else files[0][1].get("year")

    # порядок долива: сначала файл с min-год (верные year/title/sources),
    # затем остальные по убыванию обогащённости.
    def sort_key(sd):
        s, d = sd
        is_min = isinstance(d.get("year"), int) and d["year"] == canonical_year
        return (0 if is_min else 1, -richness(d))

    ordered = sorted(files, key=sort_key)

    merged: dict = {}
    for _s, d in ordered:
        for k, v in d.items():
            if k == "external_ids":
                ext = dict(merged.get("external_ids") or {})
                for ek, ev in (v or {}).items():
                    ext.setdefault(ek, ev)
                if ext:
                    merged["external_ids"] = ext
            elif k not in merged and v not in (None, "", [], {}):
                merged[k] = v

    merged["year"] = canonical_year
    title = merged.get("title_ru") or merged.get("title_original") or ""
    canonical_slug = make_film_slug(title, canonical_year)
    merged["id"] = canonical_slug
    return canonical_slug, order_fields(merged)


def update_refs(root: Path, slug_map: dict[str, str], apply: bool) -> int:
    """Переназначает ссылки в references/ и collections/. Возвращает счётчик."""
    changed = 0
    refs_dir = root / "references"
    if refs_dir.exists():
        for p in refs_dir.glob("*.yaml"):
            d = load(p)
            touched = False
            if d.get("source_film") in slug_map:
                d["source_film"] = slug_map[d["source_film"]]
                touched = True
            tgt = d.get("target") or {}
            if tgt.get("type") == "film" and tgt.get("ref") in slug_map:
                tgt["ref"] = slug_map[tgt["ref"]]
                touched = True
            if touched:
                changed += 1
                if apply:
                    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    coll_dir = root / "collections"
    if coll_dir.exists():
        for p in coll_dir.glob("*.yaml"):
            d = load(p)
            touched = False
            for key in ("films", "film_list", "items"):
                lst = d.get(key)
                if not isinstance(lst, list):
                    continue
                for i, item in enumerate(lst):
                    if isinstance(item, str) and item in slug_map:
                        lst[i] = slug_map[item]
                        touched = True
                    elif isinstance(item, dict) and item.get("film") in slug_map:
                        item["film"] = slug_map[item["film"]]
                        touched = True
            if touched:
                changed += 1
                if apply:
                    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return changed


def main(root: Path, apply: bool) -> None:
    films_dir = root / "films"
    qid_to_files: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for p in sorted(films_dir.glob("*.yaml")):
        d = load(p)
        q = (d.get("external_ids") or {}).get("wikidata")
        if q:
            qid_to_files[q].append((p.stem, d))

    dupes = {q: fs for q, fs in qid_to_files.items() if len(fs) > 1}

    slug_map: dict[str, str] = {}     # old_slug -> canonical_slug
    to_delete: set[str] = set()
    to_write: dict[str, dict] = {}    # canonical_slug -> payload
    for q, fs in dupes.items():
        canonical_slug, merged = merge_group(fs)
        to_write[canonical_slug] = merged
        for s, _d in fs:
            if s != canonical_slug:
                slug_map[s] = canonical_slug
                to_delete.add(s)

    ref_changes = update_refs(root, slug_map, apply)

    print(f"{'ПРИМЕНЕНИЕ' if apply else 'DRY-RUN'}")
    print(f"групп-дублей: {len(dupes)}")
    print(f"файлов к удалению: {len(to_delete)}")
    print(f"canonical-записей к перезаписи: {len(to_write)}")
    print(f"ссылок переназначено (refs+collections): {ref_changes}")

    if apply:
        for slug, payload in to_write.items():
            (films_dir / f"{slug}.yaml").write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        for slug in to_delete:
            f = films_dir / f"{slug}.yaml"
            if f.exists():
                f.unlink()
        print("готово.")
    else:
        sample = list(slug_map.items())[:12]
        print("\nпримеры переименований (old → canonical):")
        for old, new in sample:
            print(f"  {old}  →  {new}")


if __name__ == "__main__":
    args = sys.argv[1:]
    apply = "--apply" in args
    paths = [a for a in args if not a.startswith("--")]
    main(Path(paths[0]).resolve() if paths else Path("."), apply)
