"""
Telegram-бот: пользователь присылает запись домбры, бот отвечает нотами и MIDI-файлом.

Подготовка (один раз):
    pip install python-telegram-bot
    Создайте бота у @BotFather и положите токен в файл .env в папке проекта:
        TELEGRAM_BOT_TOKEN=123456:ABC...
    Файл .env нельзя отправлять в репозиторий и пересылать: токен даёт полный доступ к боту.

Запуск из папки проекта:
    python -m dombra_notes.bot
"""

import asyncio
import html
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import pitch

log = logging.getLogger("dombra_notes.bot")

MAX_FILE_BYTES = 20 * 1024 * 1024   # больше Telegram боту скачать не даёт
MAX_SECONDS = 180                   # длинные записи обрезаются
MAX_ROWS = 40                       # строк таблицы в одном сообщении

HELP_TEXT = (
    "Пришлите голосовое сообщение или звуковой файл с записью домбры. "
    "В ответ я назову ноты, их частоту и отклонение в центах и приложу MIDI-файл.\n\n"
    "Сейчас распознаётся только одноголосная игра: одна струна, вторая приглушена. "
    "Лучше всего получается запись в тихой комнате, телефон на расстоянии 30–50 см от инструмента."
)


def read_token() -> str:
    """Токен берётся из переменной окружения или из файла .env в текущей папке."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    env_file = Path(".env")
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
    if not token:
        raise SystemExit(
            "Не найден токен бота. Создайте бота у @BotFather и запишите токен в файл .env:\n"
            "TELEGRAM_BOT_TOKEN=ваш_токен"
        )
    return token


def analyze_path(path: Path):
    """Тяжёлая часть работы: выполняется в отдельном потоке, чтобы бот не «зависал»."""
    signal, sample_rate = pitch.load_audio(path)
    truncated = len(signal) > MAX_SECONDS * sample_rate
    if truncated:
        signal = signal[: MAX_SECONDS * sample_rate]
    return pitch.analyze(signal, sample_rate), truncated


def build_reply(notes, truncated: bool) -> str:
    if not notes:
        return pitch.format_report(notes)
    lines = [f"Найдено нот: {len(notes)}", " ".join(note.name for note in notes[:120]), ""]
    lines.append(f"{'№':>2} {'нота':<4} {'частота':>9} {'центы':>6} {'длит.':>5}")
    for index, note in enumerate(notes[:MAX_ROWS], start=1):
        lines.append(f"{index:>2} {note.name:<4} {note.frequency:>7.1f}Гц {note.cents:>+6.1f} {note.duration:>4.2f}с")
    text = "<pre>" + html.escape("\n".join(lines)) + "</pre>"
    if len(notes) > MAX_ROWS:
        text += f"\nПоказаны первые {MAX_ROWS} нот, все ноты есть в MIDI-файле."
    if truncated:
        text += f"\nЗапись длиннее {MAX_SECONDS} с: обработано только начало."
    return text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    media = message.voice or message.audio or message.document or message.video_note
    if media is None:
        return
    if media.file_size and media.file_size > MAX_FILE_BYTES:
        await message.reply_text("Файл больше 20 МБ. Пришлите запись короче или в сжатом формате.")
        return

    await message.chat.send_action(ChatAction.TYPING)
    name = getattr(media, "file_name", None) or "voice.ogg"
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / ("input" + (Path(name).suffix or ".ogg"))
        try:
            telegram_file = await media.get_file()
            await telegram_file.download_to_drive(source)
            notes, truncated = await asyncio.to_thread(analyze_path, source)
        except pitch.AudioError as error:
            await message.reply_text(f"Не удалось прочитать запись: {error}")
            return
        except Exception:
            log.exception("Сбой при обработке записи")
            await message.reply_text("При обработке записи произошла ошибка. Попробуйте другой файл.")
            return

        await message.reply_text(build_reply(notes, truncated), parse_mode="HTML")
        if notes:
            midi_path = pitch.write_midi(notes, Path(folder) / (Path(name).stem + ".mid"))
            with midi_path.open("rb") as handle:
                await message.reply_document(handle, filename=midi_path.name, caption="Ноты в формате MIDI")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).concurrent_updates(True).build()
    application.add_handler(CommandHandler(["start", "help"], start))
    audio_filter = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.Document.AUDIO | filters.Document.FileExtension("wav") \
        | filters.Document.FileExtension("m4a") | filters.Document.FileExtension("ogg") | filters.Document.FileExtension("mp3") \
        | filters.Document.FileExtension("flac")
    application.add_handler(MessageHandler(audio_filter, handle_audio))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_other))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадает адрес запроса вместе с токеном
    application = build_application(read_token())
    log.info("Бот запущен. Остановить: Ctrl+C")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
