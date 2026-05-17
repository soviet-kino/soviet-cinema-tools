"""Общие утилиты для импортёров и скриптов обогащения."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

_RU_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # украинские/белорусские
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
    "ў": "u",
}

_SLUG_RE_BAD = re.compile(r"[^a-z0-9]+")


def translit(text: str) -> str:
    """Кириллица + общая латиница → ASCII-slug-совместимый текст."""
    out: list[str] = []
    for ch in text.lower():
        if ch in _RU_TRANSLIT:
            out.append(_RU_TRANSLIT[ch])
        elif ch.isalnum() or ch in {" ", "-"}:
            out.append(ch)
    s = "".join(out)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = _SLUG_RE_BAD.sub("-", s)
    return s.strip("-")


def make_film_slug(title: str, year: int) -> str:
    """Slug фильма: nazvanie-god."""
    base = translit(title) or "untitled"
    return f"{base}-{year}"


_PATRONYMIC_SUFFIXES = ("вич", "вна")


def make_person_slug(name: str) -> str:
    """Slug персоны в формате `familiya-imya[-otchestvo]`.

    Алгоритм:
      1. Если последнее слово — отчество (оканчивается на «-вич/-вна»),
         то имя уже в порядке «Фамилия Имя Отчество» (типичный паспортный
         порядок в ru-лейблах Wikidata) — не переставляем.
      2. Иначе считаем, что фамилия последняя (порядок «Имя [Отчество]
         Фамилия» или иностранное «Given Surname»), и переносим её в начало.

    Известные ограничения: иностранные имена с предлогами (van, de la,
    von), формат «Фамилия, Имя», одиночные имена-псевдонимы. Это
    сознательный компромисс: точную канонизацию имени делает редактор;
    slug стабилен после публикации.
    """
    if not name.strip():
        return "unknown"
    parts = name.split()
    if len(parts) < 2:
        return translit(name)
    if parts[-1].lower().endswith(_PATRONYMIC_SUFFIXES):
        return translit(name)
    return translit(f"{parts[-1]} {' '.join(parts[:-1])}")


def make_studio_slug(name: str) -> str:
    return translit(name) or "studio"


def parse_qid(uri: str) -> str | None:
    """Из http://www.wikidata.org/entity/Q12345 → Q12345."""
    if not uri:
        return None
    tail = uri.rsplit("/", 1)[-1]
    return tail if tail.startswith("Q") else None


# Wikidata P18 возвращает URI вида:
#   http://commons.wikimedia.org/wiki/Special:FilePath/Andrei%20Tarkovsky.jpg
_COMMONS_PREFIX = "http://commons.wikimedia.org/wiki/Special:FilePath/"
_COMMONS_PREFIX_HTTPS = "https://commons.wikimedia.org/wiki/Special:FilePath/"


def parse_commons_filename(uri: str) -> str | None:
    """Из Special:FilePath URI достаём декодированное имя файла."""
    if not uri:
        return None
    for prefix in (_COMMONS_PREFIX_HTTPS, _COMMONS_PREFIX):
        if uri.startswith(prefix):
            return urllib.parse.unquote(uri[len(prefix):])
    return None


# ---- YAML index builders --------------------------------------------------


def load_yaml_files(directory: Path) -> dict[str, dict[str, Any]]:
    """Прочитать все *.yaml в директории. Возвращает {slug: parsed}."""
    out: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return out
    for p in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(raw, dict) and "id" in raw:
            out[raw["id"]] = raw
    return out


def index_by_qid(entities: dict[str, dict[str, Any]]) -> dict[str, str]:
    """{QID: slug} для всех сущностей с external_ids.wikidata."""
    out: dict[str, str] = {}
    for slug, body in entities.items():
        qid = (body.get("external_ids") or {}).get("wikidata")
        if isinstance(qid, str) and qid.startswith("Q"):
            out[qid] = slug
    return out


def dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def unique_slug(base: str, taken: set[str]) -> str:
    """Если base уже занят, добавляем -2, -3 и т.д."""
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"
