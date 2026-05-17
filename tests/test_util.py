"""Тесты на importers.util — особенно slug-эвристика для имён."""

from __future__ import annotations

from importers.util import (
    make_film_slug,
    make_person_slug,
    make_studio_slug,
    parse_commons_filename,
    parse_qid,
)

# ---- person slug --------------------------------------------------------


def test_person_imya_familiya():
    """«Имя Фамилия» → фамилия в начало."""
    assert make_person_slug("Андрей Тарковский") == "tarkovskiy-andrey"


def test_person_imya_otchestvo_familiya():
    """«Имя Отчество Фамилия» → фамилия в начало."""
    assert make_person_slug("Александр Аркадьевич Белинский") == "belinskiy-aleksandr-arkadevich"


def test_person_familiya_imya_otchestvo():
    """«Фамилия Имя Отчество» (паспортный порядок Wikidata) — оставляем."""
    assert make_person_slug("Лысенко Вадим Григорьевич") == "lysenko-vadim-grigorevich"


def test_person_female_patronymic():
    """Женские отчества на -вна тоже считаем за маркер паспортного порядка."""
    assert make_person_slug("Иванова Мария Сергеевна") == "ivanova-mariya-sergeevna"


def test_person_foreign_two_words():
    """Иностранные имена: «Given Surname» → «surname-given»."""
    assert make_person_slug("Andrzej Wajda") == "wajda-andrzej"
    assert make_person_slug("Bohumil Hrabal") == "hrabal-bohumil"


def test_person_single_word():
    """Псевдоним из одного слова — просто транслит."""
    assert make_person_slug("Мадонна") == "madonna"


def test_person_empty():
    assert make_person_slug("") == "unknown"
    assert make_person_slug("   ") == "unknown"


# ---- film slug ----------------------------------------------------------


def test_film_slug_ru():
    assert make_film_slug("Зеркало", 1974) == "zerkalo-1974"


def test_film_slug_with_punctuation():
    assert make_film_slug("Иваново детство", 1962) == "ivanovo-detstvo-1962"


def test_film_slug_empty_title():
    assert make_film_slug("", 1970) == "untitled-1970"


# ---- studio slug --------------------------------------------------------


def test_studio_slug():
    assert make_studio_slug("Мосфильм") == "mosfilm"


# ---- wikidata URIs ------------------------------------------------------


def test_parse_qid():
    assert parse_qid("http://www.wikidata.org/entity/Q42735") == "Q42735"
    assert parse_qid("") is None
    assert parse_qid("http://example.org/foo") is None


def test_parse_commons_filename_http():
    uri = "http://commons.wikimedia.org/wiki/Special:FilePath/Andrei%20Tarkovsky.jpg"
    assert parse_commons_filename(uri) == "Andrei Tarkovsky.jpg"


def test_parse_commons_filename_https():
    uri = "https://commons.wikimedia.org/wiki/Special:FilePath/Stalker_poster.png"
    assert parse_commons_filename(uri) == "Stalker_poster.png"


def test_parse_commons_filename_unknown():
    assert parse_commons_filename("http://example.org/file.jpg") is None
