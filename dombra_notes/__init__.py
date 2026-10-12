"""
Звук домбры → ноты и акустический профиль.

    python -m dombra_notes запись.wav                      ноты одноголосной записи (+ MIDI, JSON)
    python -m dombra_notes.session recordings/ДАТА_DOM001  папка сессии → notes.csv, profile.json
    python -m dombra_notes.bot                             Telegram-бот

Модули: pitch (высота и ноты), acoustic (профиль ноты: атака, затухание, гармоники, шум,
резонансы корпуса), session (обработка папки записей по протоколу), bot.
"""
