"""
Импорт домбр из переписки WhatsApp (чаты «Домбыра проект» и с А. Капсалыковым)
в реестр: паспорт (черновик), фото и аудио из папки Google Drive
«Домбыра дыбыстары 24-09-2026» (Студия библиотеки ЕНУ).

Запуск: python manage.py shell < import_dombras.py  (из корня проекта)
Повторный запуск безопасен: домбра ищется по названию, уже загруженный файл
(тот же SHA-256) повторно не добавляется.
"""
import os
from django.contrib.auth import get_user_model
from django.core.files import File
from django.db import transaction
from registry.hashing import sha256_file
from registry.models import Document, Dombra, Organization

HOME = os.environ["HOME"]
PHOTO_DIR = f"{HOME}/mnt/Проект Домбыра/Фото домбр"
AUDIO_DIR = f"{HOME}/mnt/Проект Домбыра/Аудио домбр (Google Drive)/Домбыра дыбыстары 24-09-2026"
DRIVE = "https://drive.google.com/drive/folders/1Irg_VwUdNSU-lSsR6Fqu6GXgZoqbZj-z"
STUDIO = "Студия библиотеки ЕНУ (звукорежиссёр Арманбек)"

user = get_user_model().objects.get(username="adilm")


def org(name, city, website=""):
    o, _ = Organization.objects.get_or_create(
        name=name, defaults={"org_type": "workshop", "city": city, "website": website}
    )
    return o


def audio(prefix, numbers, date):
    """Список (имя файла, подпись) для серии записей."""
    return [(f"{prefix}{n}.mp3", f"Студийная запись {date}, {STUDIO}, файл {n}") for n in numbers]


def notes(lines):
    return "\n".join(lines)


AUDIO_NOTE = f"Аудио: папка Google Drive «Домбыра дыбыстары 24-09-2026» — {DRIVE}"

DOMBRAS = [
    # ---------------------------------------------------------------- ДОМ-001
    dict(
        key="Домбра мастера Нурбека, 2015",
        existing=True,
        photos=[("Фото Дом#001.jpeg", "ДОМ-001: общий вид (фото владельца, 30.09.2026)")],
        audio=audio("1ДОМ Дыбыс-", range(1, 9), "24.09.2026"),
        notes_add=[
            "",
            "--- Дополнено 13.10.2026 по переписке WhatsApp ---",
            "Рабочий номер в проекте: ДОМ-001 (аудио «1ДОМ Дыбыс-1…8»).",
            "Сведения дал А. Капсалыков (исполнитель, владелец инструмента) 27.09.2026.",
            "Запись: 24.09.2026, " + STUDIO + ".",
            AUDIO_NOTE,
        ],
    ),
    # ---------------------------------------------------------------- ДОМ-002
    dict(
        key="Домбра мастера Ж.Х. Сейтахова (ДОМ-002)",
        fields=dict(
            master_name="Сейтахов Жанпейл Хайыргаливич",
            manufacturer=("Мастер Сейтахов Ж.Х.", "Орал (Уральск)"),
            manufacture_place="г. Орал (Уральск)",
            dombra_type="other",
            body_material="сибирская берёза",
            neck_material="бук, граб",
            soundboard_material="сосна",
            frets_count=19,
            total_length_mm=982,
            scale_length_mm=726,
            strings_type="нейлон",
            tuning="ре–соль (кварта)",
        ),
        photos=[
            ("Фото Дом#002.JPG.jpeg", "ДОМ-002: общий вид (фото 30.09.2026)"),
            ("Фото Дом#002 (HEIC).jpeg", "ДОМ-002: фото 27.09.2026 (из HEIC)"),
            ("Фото2 Дом#002.jpeg", "ДОМ-002: фото 27.09.2026, второй ракурс (из HEIC)"),
        ],
        audio=audio("2ДОМ Дыбыс-", [1], "24.09.2026")
        + audio("2ДОМ дауыс", range(1, 8), "25.09.2026"),
        notes=[
            "Внесено 13.10.2026 по переписке WhatsApp «Домбыра проект» (сведения Гулькерим, 27.09.2026). Статус — черновик.",
            "Рабочий номер в проекте: ДОМ-002 (аудио «2ДОМ Дыбыс-1», «2ДОМ дауыс1…7»).",
            "",
            "Мастер: Сейтахов Жанпейл Хайыргаливич, г. Орал, член Союза мастеров по изготовлению саз-аспаптар.",
            "Стоимость: 100 000 ₸.",
            "Лады: навязные (перевязанные), 19.",
            "Высота струн над 12-м ладом: 6 мм.",
            "Длина (высота) инструмента: 98,2 см.",
            "Состояние: сведений нет.",
            "",
            "Уточнить перед регистрацией: год изготовления, вес, тип домбры (сейчас «другая»), владелец.",
            "Запись: 24–25.09.2026, " + STUDIO + ". 24.09 — кюй, 25.09 — перезапись по протоколу (лады, динамика и т. д.).",
            AUDIO_NOTE,
        ],
    ),
    # ---------------------------------------------------------------- ДОМ-003
    dict(
        key="Домбра мастерской Dasturart, клён (ДОМ-003)",
        fields=dict(
            master_name="Бауыржан",
            manufacturer=("Мастерская Dasturart", "Астана"),
            manufacture_place="г. Астана",
            dombra_type="other",
            body_material="клён",
            neck_material="клён",
            soundboard_material="сосна",
            total_length_mm=945,
            scale_length_mm=715,
            strings_type="нейлон",
            tuning="ре–соль (кварта)",
            weight_g=1300,
        ),
        photos=[
            ("Фото Дом#003.jpeg", "ДОМ-003: общий вид, дека (фото 29.09.2026)"),
            ("Фото Дом#003-2.jpeg", "ДОМ-003: корпус сзади (фото 29.09.2026)"),
        ],
        audio=audio("3ДОМ дауыс ", range(1, 12), "29.09.2026")
        + audio("ДЕКАН дом дыбыс ", range(1, 10), "06.10.2026"),
        notes=[
            "Внесено 13.10.2026 по переписке WhatsApp «Домбыра проект» (сведения А. Капсалыкова, 29.09.2026). Статус — черновик.",
            "Рабочий номер в проекте: ДОМ-003 («декановская» домбра; в чате сначала названа «номер 4»).",
            "",
            "Мастер: Бауыржан, мастерская Dasturart. Куплена в Астане за 300 000 ₸.",
            "Лады: навязные (количество не указано).",
            "Высота струн над 12-м ладом: 6 мм.",
            "Состояние: новая.",
            "Владелец: Ерсұлтан, тел. 8 708 762 77 01.",
            "",
            "Аудио: «3ДОМ дауыс 1…11» — запись 29.09.2026; «ДЕКАН дом дыбыс 1…9» — запись 06.10.2026.",
            "Обе серии отнесены к этой домбре по переписке (29.09 записали домбру декана — она же Dasturart). ПРОВЕРИТЬ, что «ДЕКАН дом» — тот же инструмент.",
            "Уточнить перед регистрацией: год изготовления, количество ладов, тип домбры.",
            "Записи: " + STUDIO + ".",
            AUDIO_NOTE,
        ],
    ),
]

MUSAEV_COMMON = dict(
    master_name="Мұсаев Сұлтан",
    manufacturer=("Мастер Мұсаев Сұлтан", "Астана"),
    manufacture_year=2026,
    manufacture_place="г. Астана",
    dombra_type="other",
    soundboard_material="ель",
    frets_count=19,
    strings_type="нейлон",
    tuning="ре–соль (кварта)",
    weight_g=1300,
)

for code, wood_title, body, fingerboard, price, scale, height, length, prefix in [
    ("ДОМ-004", "канадский орех", "канадский орех", "граб", "800 000", 745, "6,8", 997, "канадский орех "),
    ("ДОМ-005", "махагони", "махагони (красное дерево, қызыл ағаш)", "граб", "650 000", 743, "7", 995, "махагон (қызыл ағаш) "),
    ("ДОМ-006", "клён", "клён (ёнка)", "пернамбук", "750 000", 744, "6,8", 996, "Ёнка (клен) "),
]:
    DOMBRAS.append(
        dict(
            key=f"Домбра мастера С. Мұсаева, {wood_title}, 2026 ({code})",
            fields=dict(
                MUSAEV_COMMON,
                body_material=body,
                neck_material=f"{body}; накладка грифа — {fingerboard}",
                total_length_mm=length,
                scale_length_mm=scale,
            ),
            photos=[],
            audio=audio(prefix, range(1, 10), "06.10.2026"),
            notes=[
                f"Внесено 13.10.2026 по переписке WhatsApp с А. Капсалыковым (сведения 07.10.2026). Статус — черновик.",
                f"Рабочий номер в проекте: {code} (аудио «{prefix.strip()} 1…9»).",
                "",
                "Мастер: Мұсаев Сұлтан, Астана, 2026 г. Привезена от мастера для записи 06.10.2026.",
                f"Стоимость: {price} ₸.",
                f"Корпус и гриф: {body}; накладка грифа: {fingerboard}. Дека: ель.",
                "Лады: навязные жильные, 19.",
                f"Высота струн над 12-м ладом: {height} мм.",
                f"Общая длина: {length} мм (в сообщении указано «{str(length / 10).replace('.', ',')}мм» — по смыслу сантиметры).",
                "Вес: 1,3 кг. Состояние: новая. Владельца нет (инструмент мастера).",
                "",
                "ФОТО НЕТ: в выгрузке чата изображения отсутствуют — запросить у А. Капсалыкова (общий вид, дека, гриф, клеймо).",
                "Уточнить перед регистрацией: тип домбры.",
                "Запись: 06.10.2026, " + STUDIO + ".",
                AUDIO_NOTE,
            ],
        )
    )


def attach(dombra, doc_type, folder, items, public):
    have = set(dombra.documents.values_list("sha256", flat=True))
    added = 0
    for filename, title in items:
        path = os.path.join(folder, filename)
        with open(path, "rb") as fh:
            digest = sha256_file(fh)
            if digest in have:
                continue
            doc = Document(
                dombra=dombra, doc_type=doc_type, title=title[:255],
                file=File(fh, name=filename), is_public=public, uploaded_by=user,
            )
            doc.full_clean(exclude=["original_name", "sha256"])
            doc.save()
            have.add(digest)
            added += 1
    return added


with transaction.atomic():
    for spec in DOMBRAS:
        if spec.get("existing"):
            d = Dombra.objects.get(title=spec["key"])
            if "ДОМ-001" not in d.internal_notes:
                d.internal_notes = d.internal_notes.rstrip() + "\n" + notes(spec["notes_add"])
                d.save()
        else:
            fields = dict(spec["fields"])
            name, city = fields.pop("manufacturer")
            fields["manufacturer"] = org(name, city)
            d, created = Dombra.objects.get_or_create(
                title=spec["key"],
                defaults=dict(fields, internal_notes=notes(spec["notes"]), created_by=user),
            )
            d.full_clean()
        p = attach(d, Document.DocType.PHOTO, PHOTO_DIR, spec["photos"], True)
        a = attach(d, Document.DocType.AUDIO, AUDIO_DIR, spec["audio"], False)
        print(f"{d.title}: фото +{p}, аудио +{a}; всего документов {d.documents.count()}")
