# soviet-cinema-tools

Скрипты валидации и импорта для проекта **Soviet Bloc Cinema**.

## Состав

| Модуль       | Назначение                                                |
|--------------|-----------------------------------------------------------|
| `validators` | Pydantic-схемы и CLI-валидатор данных `soviet-cinema-data`|
| `importers`  | Импорт из внешних источников (сейчас — Wikidata)          |
| `migrations` | Скрипты миграций при бампе `schema_version` (пока пусто)  |

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Валидация данных

```bash
sbc-validate ../soviet-cinema-data
# или
python -m validators.cli ../soviet-cinema-data
```

CI запускает то же самое на каждый PR в `soviet-cinema-data`.

## Импорт из Wikidata

Согласно CLAUDE.md (раздел «Импортируй фильмы СССР за 1970-е»):

```bash
sbc-import-films \
  --country SU \
  --year-from 1970 --year-to 1979 \
  --out ../soviet-cinema-data \
  --limit 500 \
  --dry-run
```

После предварительного просмотра запускайте без `--dry-run`. Скрипт **не**
перезаписывает существующие YAML, если не передан `--force`. Отчёт о пропусках
и недостающих людях/студиях пишется в `../soviet-cinema-data/reports/`.

Помните: не пушить десятки тысяч файлов одним PR. Разбивайте по годам или
республикам.

## Тесты

```bash
pytest
```

## Лицензия

MIT — см. `LICENSE`.
