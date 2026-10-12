"""
Проверка бота без сети: сообщения Telegram подменяются заглушками.

    python -m unittest dombra_notes.tests.test_bot -v

Проверяется логика обработки записи. Связь с серверами Telegram здесь не проверяется.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import soundfile as sf

from dombra_notes.tests import synth

try:
    from dombra_notes import bot
except ImportError:  # библиотека python-telegram-bot не установлена
    bot = None

MELODY = [50, 55, 57, 62]


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


class FakeTelegramFile:
    def __init__(self, writer):
        self.writer = writer

    async def download_to_drive(self, path):
        self.writer(Path(path))


def make_update(kind, media):
    slots = dict(voice=None, audio=None, document=None, video_note=None)
    slots[kind] = media
    sent = {}

    async def reply_document(handle, filename=None, caption=None):
        sent["filename"], sent["data"] = filename, handle.read()

    message = SimpleNamespace(
        **slots,
        chat=SimpleNamespace(send_action=AsyncMock()),
        reply_text=AsyncMock(),
        reply_document=AsyncMock(side_effect=reply_document),
    )
    return SimpleNamespace(message=message), sent


@unittest.skipIf(bot is None, "python-telegram-bot не установлен")
class BotTests(unittest.TestCase):
    def test_voice_message_gives_notes_and_midi(self):
        signal = synth.melody([(hz(m), 0.4) for m in MELODY], 48000)
        writer = lambda path: sf.write(str(path), signal, 48000, format="OGG", subtype="OPUS")
        voice = SimpleNamespace(file_size=20_000, get_file=AsyncMock(return_value=FakeTelegramFile(writer)))
        update, sent = make_update("voice", voice)

        asyncio.run(bot.handle_audio(update, None))

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Найдено нот: 4", text)
        self.assertIn("D3 G3 A3 D4", text)
        self.assertEqual(update.message.reply_text.call_args.kwargs.get("parse_mode"), "HTML")
        self.assertEqual(sent["filename"], "voice.mid")
        self.assertTrue(sent["data"].startswith(b"MThd"))

    def test_document_keeps_its_name(self):
        signal = synth.melody([(hz(m), 0.4) for m in MELODY], 44100)
        writer = lambda path: sf.write(str(path), signal, 44100)
        document = SimpleNamespace(file_size=100_000, file_name="кюй фрагмент.wav",
                                   get_file=AsyncMock(return_value=FakeTelegramFile(writer)))
        update, sent = make_update("document", document)
        asyncio.run(bot.handle_audio(update, None))
        self.assertEqual(sent["filename"], "кюй фрагмент.mid")

    def test_too_large_file_is_refused(self):
        audio = SimpleNamespace(file_size=bot.MAX_FILE_BYTES + 1, get_file=AsyncMock())
        update, _ = make_update("audio", audio)
        asyncio.run(bot.handle_audio(update, None))
        audio.get_file.assert_not_called()
        self.assertIn("20 МБ", update.message.reply_text.call_args.args[0])

    def test_unreadable_file_gives_clear_message(self):
        writer = lambda path: path.write_bytes(b"this is not audio")
        document = SimpleNamespace(file_size=17, file_name="notes.wav",
                                   get_file=AsyncMock(return_value=FakeTelegramFile(writer)))
        update, sent = make_update("document", document)
        asyncio.run(bot.handle_audio(update, None))
        self.assertIn("Не удалось прочитать запись", update.message.reply_text.call_args.args[0])
        self.assertEqual(sent, {})

    def test_silence_gives_explanation_without_midi(self):
        import numpy as np

        writer = lambda path: sf.write(str(path), np.zeros(48000), 48000, format="OGG", subtype="OPUS")
        voice = SimpleNamespace(file_size=5_000, get_file=AsyncMock(return_value=FakeTelegramFile(writer)))
        update, sent = make_update("voice", voice)
        asyncio.run(bot.handle_audio(update, None))
        self.assertIn("не найдены", update.message.reply_text.call_args.args[0])
        self.assertEqual(sent, {})

    def test_long_melody_is_shortened_in_message(self):
        notes_count = bot.MAX_ROWS + 10
        signal = synth.melody([(hz(50 + i % 12), 0.15) for i in range(notes_count)], 44100)
        writer = lambda path: sf.write(str(path), signal, 44100)
        audio = SimpleNamespace(file_size=500_000, file_name="long.wav", get_file=AsyncMock(return_value=FakeTelegramFile(writer)))
        update, sent = make_update("audio", audio)
        asyncio.run(bot.handle_audio(update, None))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn(f"Найдено нот: {notes_count}", text)
        self.assertIn("Показаны первые", text)
        self.assertLess(len(text), 4096)  # предел длины сообщения Telegram

    def test_token_from_env_file_and_application(self):
        previous = os.getcwd()
        saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                os.chdir(folder)
                with self.assertRaises(SystemExit):
                    bot.read_token()
                Path(".env").write_text('OTHER=1\nTELEGRAM_BOT_TOKEN="123456:TEST-token"\n', encoding="utf-8")
                token = bot.read_token()
                self.assertEqual(token, "123456:TEST-token")
                application = bot.build_application(token)  # сеть не используется
                self.assertEqual(len(application.handlers[0]), 3)
        finally:
            os.chdir(previous)
            if saved is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = saved


if __name__ == "__main__":
    unittest.main()
