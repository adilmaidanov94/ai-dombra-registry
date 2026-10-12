"""
Хэширование для реестра домбр.

Здесь собрано всё, от чего зависит воспроизводимость хэшей:
  * SHA-256 файлов (чтение кусками, чтобы не держать аудио целиком в памяти);
  * канонический JSON: одни и те же данные всегда дают одну и ту же строку
    байтов, а значит, один и тот же хэш.

ВАЖНО: правила из этого файла нельзя менять «на ходу». Если формат расчёта
понадобится изменить, нужно увеличить HASH_VERSION и оставить старый алгоритм
для проверки уже записанных событий.
"""

import hashlib
import json

# Хэш «предыдущего события» для самого первого события в цепочке.
GENESIS_HASH = "0" * 64

# Версия алгоритма расчёта хэшей события. Хранится в каждом событии.
HASH_VERSION = 1

CHUNK_SIZE = 1024 * 1024  # 1 МБ


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(file_obj) -> str:
    """
    SHA-256 содержимого файла.

    Принимает как файлы Django (UploadedFile, FieldFile), так и обычные
    файловые объекты, открытые в двоичном режиме.
    """
    digest = hashlib.sha256()
    if hasattr(file_obj, "chunks"):
        # Файлы Django: chunks() сам перематывает файл в начало.
        for chunk in file_obj.chunks(CHUNK_SIZE):
            digest.update(chunk)
    else:
        file_obj.seek(0)
        for chunk in iter(lambda: file_obj.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except (OSError, ValueError):
            pass
    return digest.hexdigest()


def canonical_json(data) -> bytes:
    """
    Каноническое представление данных для хэширования.

    Ключи сортируются, пробелов нет, кириллица не экранируется, кодировка
    UTF-8. Допустимы только типы JSON (dict, list, str, int, bool, None);
    даты, UUID и Decimal нужно заранее превращать в строки, а дробных чисел
    лучше избегать совсем: их запись зависит от платформы.
    """
    return json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(data) -> str:
    return sha256_bytes(canonical_json(data))
