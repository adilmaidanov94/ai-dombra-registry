# AI Dombra Registry — реестр домбр

Веб-реестр домбр с цифровым паспортом инструмента (Django): регистрационный номер,
мастер и материалы, файлы (фото, аудио, сертификаты) с SHA-256, неизменяемая
история событий (цепочка хэшей). Следующие этапы: защита записей → блокчейн.

## Запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

Реестр: http://127.0.0.1:8000/admin/registry/dombra/

## Состав

- `registry/` — модели, бизнес-логика (`services.record_event`), проверка целостности (`manage.py verify_registry`)
- `db.sqlite3`, `media/` — данные реестра: 6 домбр (ДОМ-001…006, черновики) с фото и студийными записями
- `tools/import_dombras_v1.py` — импорт домбр из переписки WhatsApp и папки аудио Google Drive
- `dombra_notes/` — распознавание нот домбры (звук → нота, Telegram-бот)
- `recordings/` — протокол студийной записи
- `presentation/`, `Claude outputs/` — презентации проекта

Репозиторий приватный: в базе есть цены и контакты владельцев инструментов.
