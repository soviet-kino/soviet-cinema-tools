"""Smoke-тесты на схему. Запуск: `pytest` из корня soviet-cinema-tools."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from validators.schemas import (
    CastEntry,
    Film,
    Motif,
    Person,
    Reference,
    Studio,
)


def test_film_minimal_ok():
    f = Film(
        id="zerkalo-1974",
        title_ru="Зеркало",
        title_original="Зеркало",
        year=1974,
        country=["SU"],
    )
    assert f.id == "zerkalo-1974"
    assert f.schema_version == 1


def test_film_rejects_bad_slug():
    with pytest.raises(ValidationError):
        Film(
            id="Zerkalo 1974",
            title_ru="Зеркало",
            title_original="Зеркало",
            year=1974,
            country=["SU"],
        )


def test_film_rejects_extra_field():
    with pytest.raises(ValidationError):
        Film(
            id="zerkalo-1974",
            title_ru="Зеркало",
            title_original="Зеркало",
            year=1974,
            country=["SU"],
            director_chair="comfortable",  # лишнее поле
        )


def test_film_year_out_of_range():
    with pytest.raises(ValidationError):
        Film(
            id="x-1850",
            title_ru="x",
            title_original="x",
            year=1850,
            country=["SU"],
        )


def test_person_partial_date_ok():
    Person(id="x", name_ru="X", birth="1932")
    Person(id="y", name_ru="Y", birth="1932-04")
    Person(id="z", name_ru="Z", birth="1932-04-04")


def test_person_partial_date_bad():
    with pytest.raises(ValidationError):
        Person(id="x", name_ru="X", birth="4 апреля 1932")


def test_studio_country_required():
    with pytest.raises(ValidationError):
        Studio(id="x", name_ru="X")  # type: ignore[call-arg]


def test_motif_ok():
    Motif(id="rain", name_ru="Дождь", description_ru="…", category=["visual"])


def test_reference_film_to_film_ok():
    Reference(
        id="a-to-b",
        source_film="a-1970",
        target={"type": "film", "ref": "b-1971"},
        kind="thematic",
        description_ru="…",
        confidence="medium",
    )


def test_reference_book_target_requires_title():
    with pytest.raises(ValidationError):
        Reference(
            id="a-to-book",
            source_film="a-1970",
            target={"type": "book"},  # нет title_original
            kind="adaptation",
            description_ru="…",
            confidence="high",
        )


def test_cast_entry_ok():
    CastEntry(person="terekhova-margarita", role="Мать")
