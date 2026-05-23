"""Pydantic-модели для всех сущностей soviet-cinema-data.

Согласно CLAUDE.md: схема версионируется в поле `schema_version`. Несовместимые
изменения требуют отдельного PR с миграционным скриптом в
`soviet-cinema-tools/migrations/`.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_SCHEMA_VERSION = 1

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PARTIAL_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

Slug = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        description="kebab-case на латинице, стабильный после публикации",
    ),
]


class StrictModel(BaseModel):
    """База: запрещает лишние поля, нормализует строки."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExternalIds(StrictModel):
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    imdb: str | None = Field(default=None, pattern=r"^tt\d+$")
    tmdb: int | str | None = None
    kinopoisk: int | str | None = None
    # YouTube video ID (Wikidata P1651). Конкретное видео фильма —
    # например, с официального канала «Мосфильма».
    youtube: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{6,32}$")


# ---------- studio ----------


class Studio(StrictModel):
    id: Slug
    name_ru: str
    name_original: str | None = None
    name_translit: str | None = None
    country: str  # country code из vocabularies/countries.yaml
    founded: int | None = Field(default=None, ge=1890, le=2000)
    dissolved: int | None = Field(default=None, ge=1890, le=2100)
    # Имя файла на Wikimedia Commons (Wikidata P18). Используется как
    # `https://commons.wikimedia.org/wiki/Special:FilePath/<filename>?width=…`.
    image_commons: str | None = None
    external_ids: ExternalIds | None = None
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- person ----------


class Person(StrictModel):
    id: Slug
    name_ru: str
    name_original: str | None = None
    name_translit: str | None = None
    # YAML по умолчанию приведёт `1932-04-04` к datetime.date, поэтому
    # принимаем оба варианта и нормализуем к строке.
    birth: str | None = None  # YYYY | YYYY-MM | YYYY-MM-DD
    death: str | None = None
    nationality: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    # Имя файла на Wikimedia Commons (Wikidata P18).
    image_commons: str | None = None
    external_ids: ExternalIds | None = None
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION

    @field_validator("birth", "death", mode="before")
    @classmethod
    def _coerce_partial_date(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, int):
            return f"{v:04d}"
        if isinstance(v, str):
            if not PARTIAL_DATE_RE.match(v):
                raise ValueError(f"дата '{v}' должна быть YYYY, YYYY-MM или YYYY-MM-DD")
            return v
        raise ValueError(f"непонятный тип даты: {type(v).__name__}")


# ---------- film ----------


class CastEntry(StrictModel):
    person: Slug
    role: str | None = None


ColorMode = Literal["color", "bw", "color_and_bw"]
ProductionStatus = Literal["released", "shelved", "unreleased", "unfinished", "lost"]


class SovietRelease(StrictModel):
    """Информация о советском прокате зарубежного фильма.

    Зарубежные фильмы импортировались Госкино и выходили в советский
    прокат с переводом, дубляжом, иногда сокращённой цензором. Эта
    информация — отдельная сущность от исходной даты выхода (release_date).
    """

    year: int = Field(ge=1922, le=1991, description="Год выхода в советском прокате")
    title_ru: str | None = Field(
        default=None,
        description="Название, под которым фильм шёл в СССР (может отличаться)",
    )
    dubbed: bool | None = Field(
        default=None,
        description="Был ли полный дубляж (true) или субтитры/закадровый (false)",
    )
    notes: str | None = Field(
        default=None,
        description="Сокращения цензуры, особенности проката и т.п.",
    )


class Film(StrictModel):
    id: Slug
    title_ru: str
    title_original: str
    title_translit: str | None = None
    title_en: str | None = None
    year: int = Field(ge=1900, le=2030)
    country: list[str] = Field(min_length=1)
    republic: str | None = None  # обязательно осмысленно только для country=[SU]
    studio: list[Slug] = Field(default_factory=list)
    director: list[Slug] = Field(default_factory=list)
    screenwriter: list[Slug] = Field(default_factory=list)
    cinematographer: list[Slug] = Field(default_factory=list)
    composer: list[Slug] = Field(default_factory=list)
    cast: list[CastEntry] = Field(default_factory=list)
    runtime_min: int | None = Field(default=None, gt=0, lt=1500)
    language: list[str] = Field(default_factory=list)
    genre: list[str] = Field(default_factory=list)
    color: ColorMode | None = None
    release_date: date | str | None = None
    production_status: ProductionStatus | None = None
    censorship_status: str | None = None
    poster_tmdb_path: str | None = None
    # Фоновый кадр от TMDB (для hero-секций на странице фильма).
    backdrop_tmdb_path: str | None = None
    # Постер на Wikimedia Commons (Wikidata P3383/P18) — имя файла.
    poster_commons: str | None = None
    # Тематические разделы для исследования (хрононавтика, эзопов язык,
    # киноокраина и т.д.). Slug-и из topics/.
    topics: list[Slug] = Field(default_factory=list)
    # Советский прокат — заполняется для зарубежных фильмов, выходивших
    # в СССР. Для собственно советских фильмов поле игнорируется (год
    # совпадает с year / release_date).
    soviet_release: SovietRelease | None = None
    external_ids: ExternalIds | None = None
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- topic ----------


class TopicFilter(StrictModel):
    """Декларативный фильтр для динамического подбора фильмов в тему.

    Все поля опциональны и комбинируются как AND. Если у фильма есть
    `topics: [<id>]`, он попадает в тему и без фильтра (явная привязка).
    """

    year_from: int | None = Field(default=None, ge=1900, le=2030)
    year_to: int | None = Field(default=None, ge=1900, le=2030)
    director: Slug | None = None
    screenwriter: Slug | None = None
    composer: Slug | None = None  # ключевой композитор фильма
    book_author: Slug | None = None  # автор первоисточника (через references)
    country: str | None = None


class Topic(StrictModel):
    """Тематический раздел: содержательная категория для подборки и разбора.

    В отличие от мотива (повторяющийся образ/приём) топик — это
    исследовательская тема, по которой группируется кураторская подборка
    фильмов и эссе. Пример: «хрононавтика» — фильмы о путешествиях во
    времени, петлях времени и философии времени.

    Подборка фильмов формируется двумя способами:
      1. Явная привязка: у фильма указан этот topic в `topics`.
      2. Декларативный `filter` — фильмы автоматически вычисляются по
         режиссёру / сценаристу / автору книги / периоду.

    Оба способа объединяются (OR) — фильм попадает в тему, если он
    привязан явно ИЛИ удовлетворяет фильтру.
    """

    id: Slug
    name_ru: str
    name_original: str | None = None
    description_ru: str
    long_description_ru: str | None = None
    related_motifs: list[Slug] = Field(default_factory=list)
    filter: TopicFilter | None = None
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- motif ----------


class Motif(StrictModel):
    id: Slug
    name_ru: str
    description_ru: str
    category: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- reference ----------


class ReferenceTargetFilm(StrictModel):
    type: Literal["film"]
    ref: Slug


class ReferenceTargetExternalFilm(StrictModel):
    type: Literal["external_film"]
    title_ru: str | None = None
    title_original: str
    year: int | None = None
    country: str | None = None
    director: str | None = None
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")


class ReferenceTargetBook(StrictModel):
    type: Literal["book"]
    title_ru: str | None = None
    title_original: str
    authors: list[Slug] = Field(default_factory=list)
    year: int | None = None


ReferenceTarget = Annotated[
    ReferenceTargetFilm | ReferenceTargetExternalFilm | ReferenceTargetBook,
    Field(discriminator="type"),
]

Confidence = Literal["high", "medium", "speculative"]


class ReferenceSource(StrictModel):
    essay: Slug | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> ReferenceSource:
        if self.essay is None and self.url is None:
            raise ValueError("source: укажите essay или url")
        return self


class Reference(StrictModel):
    id: Slug
    source_film: Slug
    target: ReferenceTarget
    kind: str
    description_ru: str
    confidence: Confidence
    sources: list[ReferenceSource | str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- essay (frontmatter) ----------


class Essay(StrictModel):
    id: Slug
    title: str
    author: Slug
    films: list[Slug] = Field(default_factory=list)
    motifs: list[Slug] = Field(default_factory=list)
    references: list[Slug] = Field(default_factory=list)
    published: date | None = None
    reading_time_min: int | None = Field(default=None, gt=0, lt=600)
    license: str = "CC-BY-NC-SA-4.0"
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------- collection (курированный список людей) ----------


class Collection(StrictModel):
    """Курированная подборка — людей, фильмов или того и другого.

    В отличие от Topic, который про фильмы (опционально + filter),
    Collection — это «выставка» с явным редакторским списком: например
    «Выдающиеся актёры советского кино» = list of people slugs.
    """

    id: Slug
    name_ru: str
    description_ru: str
    long_description_ru: str | None = None
    people: list[Slug] = Field(default_factory=list)
    films: list[Slug] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    schema_version: int = CURRENT_SCHEMA_VERSION


ENTITY_MODELS: dict[str, type[StrictModel]] = {
    "films": Film,
    "people": Person,
    "studios": Studio,
    "motifs": Motif,
    "references": Reference,
    "topics": Topic,
    "collections": Collection,
}
