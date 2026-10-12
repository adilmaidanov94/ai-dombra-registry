"""
Модели реестра домбр.

Четыре основных объекта:
  * Organization  — мастерская, лаборатория или орган сертификации;
  * Dombra        — инструмент и его цифровой паспорт;
  * Document      — файл (фото, аудио, сертификат...) с хэшем SHA-256;
  * RegistryEvent — запись в истории инструмента, звено цепочки хэшей.

Вспомогательная таблица RegistrationCounter хранит счётчик регистрационных
номеров по годам.

Главное правило: статус домбры и события меняются только через
registry.services.record_event(). События никогда не редактируются и не
удаляются.
"""

import datetime
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from .hashing import HASH_VERSION, sha256_file, sha256_json


class ImmutableRecordError(Exception):
    """Попытка изменить или удалить запись, защищённую от изменений."""


def current_year() -> int:
    return timezone.localdate().year


# ---------------------------------------------------------------------------
# Организация
# ---------------------------------------------------------------------------


class Organization(models.Model):
    class OrgType(models.TextChoices):
        WORKSHOP = "workshop", "Мастерская"
        LABORATORY = "laboratory", "Лаборатория"
        CERTIFICATION_BODY = "certification_body", "Орган сертификации"
        OTHER = "other", "Другое"

    name = models.CharField("Название", max_length=255, unique=True)
    org_type = models.CharField("Тип", max_length=32, choices=OrgType.choices)
    city = models.CharField("Город", max_length=120, blank=True)
    address = models.CharField("Адрес", max_length=255, blank=True)
    phone = models.CharField("Телефон", max_length=64, blank=True)
    email = models.EmailField("Эл. почта", blank=True)
    website = models.URLField("Сайт", blank=True)
    accreditation_number = models.CharField(
        "Номер аттестата аккредитации", max_length=64, blank=True
    )
    is_active = models.BooleanField("Действует", default=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "организация"
        verbose_name_plural = "организации"
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Счётчик регистрационных номеров
# ---------------------------------------------------------------------------


class RegistrationCounter(models.Model):
    """Последний выданный порядковый номер за год (DOM-KZ-2026-000001 и т. д.)."""

    year = models.PositiveSmallIntegerField("Год", primary_key=True)
    last_value = models.PositiveIntegerField("Последний номер", default=0)

    class Meta:
        verbose_name = "счётчик номеров"
        verbose_name_plural = "счётчики номеров"

    def __str__(self):
        return f"{self.year}: {self.last_value}"

    @classmethod
    def next_value(cls, year: int) -> int:
        """Выдать следующий номер. Вызывать только внутри transaction.atomic()."""
        counter, _ = cls.objects.select_for_update().get_or_create(year=year)
        counter.last_value += 1
        counter.save(update_fields=["last_value"])
        return counter.last_value


# ---------------------------------------------------------------------------
# Домбра
# ---------------------------------------------------------------------------


class Dombra(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        REGISTERED = "registered", "Зарегистрирована"
        SUBMITTED = "submitted", "Передана на проверку"
        TESTED = "tested", "Испытана"
        CERTIFIED = "certified", "Сертифицирована"
        SUSPENDED = "suspended", "Сертификат приостановлен"
        RECHECK_REQUIRED = "recheck_required", "Требуется повторная проверка"

    class DombraType(models.TextChoices):
        WESTERN = "western", "Западная (төкпе)"
        EASTERN = "eastern", "Восточная (шертпе)"
        THREE_STRING = "three_string", "Трёхструнная"
        ORCHESTRAL = "orchestral", "Оркестровая"
        CHILDREN = "children", "Детская"
        SOUVENIR = "souvenir", "Сувенирная"
        OTHER = "other", "Другая"

    # Поля паспорта. После регистрации они «замораживаются»: их значения
    # входят в хэш события регистрации и больше не редактируются.
    PASSPORT_FIELDS = (
        "title",
        "master_name",
        "manufacturer",
        "manufacture_year",
        "manufacture_date",
        "manufacture_place",
        "dombra_type",
        "body_material",
        "neck_material",
        "soundboard_material",
        "frets_count",
        "total_length_mm",
        "scale_length_mm",
        "strings_type",
        "tuning",
        "weight_g",
    )

    # --- идентификация ---
    id = models.UUIDField("UUID", primary_key=True, default=uuid.uuid4, editable=False)
    registration_number = models.CharField(
        "Регистрационный номер",
        max_length=32,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="Присваивается при регистрации, например DOM-KZ-2026-000001.",
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
        editable=False,
        db_index=True,
    )
    registered_at = models.DateTimeField("Дата регистрации", null=True, blank=True, editable=False)

    # --- паспорт ---
    title = models.CharField("Название или модель", max_length=255)
    master_name = models.CharField("ФИО мастера", max_length=255)
    manufacturer = models.ForeignKey(
        Organization,
        verbose_name="Производитель",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="manufactured_dombras",
    )
    manufacture_year = models.PositiveSmallIntegerField(
        "Год изготовления",
        null=True,
        blank=True,
        validators=[MinValueValidator(1800), MaxValueValidator(current_year)],
    )
    manufacture_date = models.DateField(
        "Точная дата изготовления",
        null=True,
        blank=True,
        help_text="Необязательно. Если известен только год, оставьте поле пустым.",
    )
    manufacture_place = models.CharField("Место изготовления", max_length=255, blank=True)
    dombra_type = models.CharField("Тип домбры", max_length=32, choices=DombraType.choices)
    body_material = models.CharField("Материал корпуса", max_length=120, blank=True)
    neck_material = models.CharField("Материал грифа", max_length=120, blank=True)
    soundboard_material = models.CharField("Материал деки", max_length=120, blank=True)
    frets_count = models.PositiveSmallIntegerField("Количество ладов", null=True, blank=True)
    total_length_mm = models.PositiveIntegerField("Длина инструмента, мм", null=True, blank=True)
    scale_length_mm = models.PositiveIntegerField("Длина мензуры, мм", null=True, blank=True)
    strings_type = models.CharField(
        "Тип струн", max_length=120, blank=True, help_text="Например: нейлон, жильные."
    )
    tuning = models.CharField(
        "Строй", max_length=120, blank=True, help_text="Например: ре–соль (кварта)."
    )
    weight_g = models.PositiveIntegerField("Вес, г", null=True, blank=True)

    # --- сертификация (заполняется действием «Выдать сертификат») ---
    certificate_number = models.CharField(
        "Номер сертификата", max_length=64, blank=True, editable=False
    )
    certification_date = models.DateField(
        "Дата сертификации", null=True, blank=True, editable=False
    )
    certified_by = models.ForeignKey(
        Organization,
        verbose_name="Кем выдан сертификат",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="certified_dombras",
    )

    # --- публикация и служебные сведения ---
    is_public = models.BooleanField(
        "Публичный паспорт",
        default=False,
        help_text="Если включено, паспорт будет виден на публичной странице проверки.",
    )
    internal_notes = models.TextField(
        "Служебные заметки", blank=True, help_text="Не показываются на публичной странице."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто создал",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Изменена", auto_now=True)

    class Meta:
        verbose_name = "домбра"
        verbose_name_plural = "домбры"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["certificate_number"],
                condition=~models.Q(certificate_number=""),
                name="uniq_certificate_number",
            ),
        ]

    def __str__(self):
        return f"{self.registration_number or 'Черновик'} — {self.title}"

    # --- удобные свойства ---

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def certification_status(self) -> str:
        """Статус сертификации для паспорта (выводится из статуса жизненного цикла)."""
        mapping = {
            self.Status.CERTIFIED: "Сертифицирована",
            self.Status.SUSPENDED: "Сертификат приостановлен",
            self.Status.RECHECK_REQUIRED: "Требуется повторная проверка",
        }
        return mapping.get(self.status, "Не сертифицирована")

    # --- проверка данных ---

    def clean(self):
        errors = {}
        today = timezone.localdate()
        if self.manufacture_date:
            if self.manufacture_date > today:
                errors["manufacture_date"] = "Дата изготовления не может быть в будущем."
            elif not self.manufacture_year:
                self.manufacture_year = self.manufacture_date.year
            elif self.manufacture_year != self.manufacture_date.year:
                errors["manufacture_year"] = "Год не совпадает с точной датой изготовления."
        if (
            self.scale_length_mm
            and self.total_length_mm
            and self.scale_length_mm >= self.total_length_mm
        ):
            errors["scale_length_mm"] = "Мензура должна быть короче всего инструмента."
        if errors:
            raise ValidationError(errors)

    # --- паспорт и его защита ---

    def passport_snapshot(self) -> dict:
        """Снимок паспорта для хэширования: только типы JSON, без дробных чисел."""
        return {
            "registration_number": self.registration_number,
            "title": self.title,
            "master_name": self.master_name,
            "manufacturer_id": self.manufacturer_id,
            "manufacturer_name": self.manufacturer.name if self.manufacturer_id else "",
            "manufacture_year": self.manufacture_year,
            "manufacture_date": (
                self.manufacture_date.isoformat() if self.manufacture_date else None
            ),
            "manufacture_place": self.manufacture_place,
            "dombra_type": self.dombra_type,
            "body_material": self.body_material,
            "neck_material": self.neck_material,
            "soundboard_material": self.soundboard_material,
            "frets_count": self.frets_count,
            "total_length_mm": self.total_length_mm,
            "scale_length_mm": self.scale_length_mm,
            "strings_type": self.strings_type,
            "tuning": self.tuning,
            "weight_g": self.weight_g,
        }

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self._guard_frozen_passport(kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def _guard_frozen_passport(self, update_fields=None):
        """После регистрации поля паспорта менять нельзя."""
        attnames = [self._meta.get_field(name).attname for name in self.PASSPORT_FIELDS]
        if update_fields is not None:
            wanted = set(update_fields)
            attnames = [
                attname
                for name, attname in zip(self.PASSPORT_FIELDS, attnames)
                if name in wanted or attname in wanted
            ]
            if not attnames:
                return
        stored = Dombra.objects.filter(pk=self.pk).values("status", *attnames).first()
        if not stored or stored["status"] == self.Status.DRAFT:
            return
        changed = [name for name in attnames if stored[name] != getattr(self, name)]
        if changed:
            raise ImmutableRecordError(
                "Паспорт зарегистрированной домбры нельзя изменять. "
                f"Изменённые поля: {', '.join(changed)}."
            )


# ---------------------------------------------------------------------------
# Документ
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "heic")
AUDIO_EXTENSIONS = ("wav", "mp3", "flac", "m4a", "ogg", "aac")
PAPER_EXTENSIONS = ("pdf", "doc", "docx", "xls", "xlsx")
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS + AUDIO_EXTENSIONS + PAPER_EXTENSIONS


def document_upload_path(instance, filename: str) -> str:
    return f"dombras/{instance.dombra_id}/{instance.doc_type}/{filename}"


def max_upload_bytes() -> int:
    return int(getattr(settings, "REGISTRY_MAX_UPLOAD_MB", 200)) * 1024 * 1024


def validate_document_file(doc_type: str, file) -> None:
    """Проверка файла: расширение должно подходить к типу документа, размер — к лимиту."""
    extension = os.path.splitext(file.name or "")[1].lower().lstrip(".")
    allowed = {
        Document.DocType.PHOTO: IMAGE_EXTENSIONS,
        Document.DocType.AUDIO: AUDIO_EXTENSIONS,
        Document.DocType.OTHER: ALLOWED_EXTENSIONS,
    }.get(doc_type, PAPER_EXTENSIONS + IMAGE_EXTENSIONS)
    if extension not in allowed:
        label = dict(Document.DocType.choices).get(doc_type, doc_type)
        raise ValidationError(
            f"Для типа «{label}» допустимы файлы: {', '.join(allowed)}. "
            f"Загружен файл с расширением «{extension or 'нет'}»."
        )
    size = getattr(file, "size", None)
    if size is not None and size > max_upload_bytes():
        raise ValidationError(
            f"Файл больше {max_upload_bytes() // (1024 * 1024)} МБ. "
            "Лимит задаётся настройкой REGISTRY_MAX_UPLOAD_MB."
        )
    if size == 0:
        raise ValidationError("Файл пустой.")


class DocumentQuerySet(models.QuerySet):
    def delete(self):
        if self.filter(event__isnull=False).exists():
            raise ImmutableRecordError(
                "Документы, записанные в историю реестра, удалять нельзя."
            )
        return super().delete()


class Document(models.Model):
    class DocType(models.TextChoices):
        PHOTO = "photo", "Фотография"
        AUDIO = "audio", "Аудиозапись звучания"
        CERTIFICATE = "certificate", "Сертификат"
        PROTOCOL = "protocol", "Протокол проверки или испытаний"
        REPAIR_ACT = "repair_act", "Акт ремонта"
        OTHER = "other", "Другое"

    dombra = models.ForeignKey(
        Dombra, verbose_name="Домбра", on_delete=models.CASCADE, related_name="documents"
    )
    event = models.ForeignKey(
        "RegistryEvent",
        verbose_name="Событие",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text="Событие реестра, которым зафиксирован документ.",
    )
    doc_type = models.CharField("Тип", max_length=32, choices=DocType.choices)
    title = models.CharField("Описание", max_length=255, blank=True)
    file = models.FileField(
        "Файл",
        upload_to=document_upload_path,
        max_length=500,
        validators=[FileExtensionValidator(ALLOWED_EXTENSIONS)],
    )
    original_name = models.CharField("Исходное имя файла", max_length=255, editable=False)
    size = models.PositiveBigIntegerField("Размер, байт", default=0, editable=False)
    sha256 = models.CharField("SHA-256", max_length=64, editable=False, db_index=True)
    is_public = models.BooleanField(
        "Публичный",
        default=False,
        help_text=(
            "Показывать документ в публичном паспорте. Фотографии и сертификат "
            "обычно публичны, протоколы — нет."
        ),
    )
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто загрузил",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="+",
    )

    objects = DocumentQuerySet.as_manager()

    class Meta:
        verbose_name = "документ"
        verbose_name_plural = "документы"
        ordering = ["dombra", "uploaded_at", "id"]

    def __str__(self):
        return f"{self.get_doc_type_display()}: {self.original_name or self.file.name}"

    @property
    def is_locked(self) -> bool:
        """Документ записан в историю и больше не меняется."""
        return self.event_id is not None

    def manifest(self) -> dict:
        """Сведения о документе, которые попадают в хэш события."""
        return {
            "sha256": self.sha256,
            "doc_type": self.doc_type,
            "original_name": self.original_name,
            "size": self.size,
        }

    def compute_file_hash(self) -> str:
        """Пересчитать SHA-256 по файлу, лежащему в хранилище."""
        with self.file.open("rb") as stored:
            return sha256_file(stored)

    def clean(self):
        if not self.file:
            return
        if self._state.adding:
            validate_document_file(self.doc_type, self.file)
            return
        stored = (
            Document.objects.filter(pk=self.pk).values("file", "doc_type", "event_id").first()
        )
        if not stored:
            return
        if self.file.name != stored["file"]:
            raise ValidationError(
                {"file": "Файл документа заменить нельзя: удалите документ и загрузите новый."}
            )
        if stored["event_id"] is not None and self.doc_type != stored["doc_type"]:
            raise ValidationError(
                {"doc_type": "Тип документа, записанного в историю, изменить нельзя."}
            )

    def save(self, *args, **kwargs):
        if self._state.adding:
            # Хэш считается один раз, в момент загрузки.
            self.original_name = os.path.basename(self.file.name)[:255]
            self.size = self.file.size
            self.sha256 = sha256_file(self.file)
        else:
            self._guard_immutable_fields(kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def _guard_immutable_fields(self, update_fields=None):
        stored = (
            Document.objects.filter(pk=self.pk)
            .values("dombra_id", "doc_type", "file", "original_name", "size", "sha256", "event_id")
            .first()
        )
        if not stored:
            return
        always_frozen = ["dombra_id", "file", "original_name", "size", "sha256"]
        frozen = always_frozen + (["doc_type"] if stored["event_id"] is not None else [])
        if update_fields is not None:
            wanted = set(update_fields) | {f"{name}_id" for name in update_fields}
            frozen = [name for name in frozen if name in wanted]
        current = {
            "dombra_id": self.dombra_id,
            "doc_type": self.doc_type,
            "file": self.file.name,
            "original_name": self.original_name,
            "size": self.size,
            "sha256": self.sha256,
        }
        changed = [name for name in frozen if stored[name] != current[name]]
        if stored["event_id"] is not None and self.event_id != stored["event_id"]:
            changed.append("event_id")
        if changed:
            raise ImmutableRecordError(
                f"Документ защищён от изменений. Изменённые поля: {', '.join(changed)}."
            )

    def delete(self, *args, **kwargs):
        if self.is_locked:
            raise ImmutableRecordError(
                "Документ записан в историю реестра, удалить его нельзя."
            )
        return super().delete(*args, **kwargs)


@receiver(post_delete, sender=Document)
def remove_document_file(sender, instance, **kwargs):
    """Удалённый черновой документ не должен оставлять файл в хранилище."""
    if not instance.file:
        return
    storage, name = instance.file.storage, instance.file.name
    transaction.on_commit(lambda: storage.delete(name))


# ---------------------------------------------------------------------------
# Событие реестра
# ---------------------------------------------------------------------------


def canonical_datetime(value: datetime.datetime) -> str:
    """
    Дата и время для хэша: всегда UTC, точность — миллисекунды.

    Миллисекунды выбраны не случайно: стандартная выгрузка Django (dumpdata)
    сохраняет время именно с такой точностью. Поэтому хэши останутся верными
    и после переноса базы из SQLite в PostgreSQL через dumpdata/loaddata.
    """
    return value.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds")


def registry_now() -> datetime.datetime:
    """Текущее время, округлённое вниз до миллисекунд (см. canonical_datetime)."""
    now = timezone.now()
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class RegistryEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError("События реестра нельзя изменять.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableRecordError("События реестра нельзя изменять.")

    def delete(self):
        raise ImmutableRecordError("События реестра нельзя удалять.")


class RegistryEvent(models.Model):
    class EventType(models.TextChoices):
        REGISTERED = "registered", "Регистрация"
        SUBMITTED = "submitted", "Передача на проверку"
        INSPECTION = "inspection", "Проверка"
        TESTED = "tested", "Испытание"
        CERTIFIED = "certified", "Выдача сертификата"
        SUSPENDED = "suspended", "Приостановка сертификата"
        RECHECK_REQUIRED = "recheck_required", "Направление на повторную проверку"
        REPAIR = "repair", "Ремонт"
        DOCUMENT_ADDED = "document_added", "Добавление документа"

    dombra = models.ForeignKey(
        Dombra, verbose_name="Домбра", on_delete=models.PROTECT, related_name="events"
    )
    sequence = models.PositiveIntegerField("№ в цепочке")
    event_type = models.CharField("Тип события", max_length=32, choices=EventType.choices)
    created_at = models.DateTimeField("Дата и время")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_by_name = models.CharField(
        "Имя автора на момент записи", max_length=150
    )
    organization = models.ForeignKey(
        Organization,
        verbose_name="Организация",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="events",
    )
    description = models.TextField("Описание", blank=True)
    status_from = models.CharField("Статус до", max_length=32, choices=Dombra.Status.choices)
    status_to = models.CharField("Статус после", max_length=32, choices=Dombra.Status.choices)
    payload = models.JSONField("Данные события", default=dict)
    hash_version = models.PositiveSmallIntegerField("Версия алгоритма", default=HASH_VERSION)
    previous_hash = models.CharField("Хэш предыдущего события", max_length=64)
    data_hash = models.CharField("Хэш данных", max_length=64)
    event_hash = models.CharField("Хэш события", max_length=64, unique=True)

    objects = RegistryEventQuerySet.as_manager()

    class Meta:
        verbose_name = "событие реестра"
        verbose_name_plural = "события реестра"
        ordering = ["dombra", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["dombra", "sequence"], name="uniq_event_sequence_per_dombra"
            ),
        ]

    def __str__(self):
        return f"#{self.sequence} {self.get_event_type_display()}"

    # --- расчёт хэшей ---

    def document_hashes(self) -> list:
        return [item["sha256"] for item in self.payload.get("documents", [])]

    def hash_input(self) -> dict:
        """Всё, что входит в хэш события (шаг 10 плана)."""
        return {
            "hash_version": self.hash_version,
            "dombra_id": str(self.dombra_id),
            "sequence": self.sequence,
            "event_type": self.event_type,
            "created_at": canonical_datetime(self.created_at),
            "created_by_id": self.created_by_id,
            "created_by_name": self.created_by_name,
            "organization_id": self.organization_id,
            "description": self.description,
            "status_from": self.status_from,
            "status_to": self.status_to,
            "document_hashes": self.document_hashes(),
            "data_hash": self.data_hash,
            "previous_hash": self.previous_hash,
        }

    def compute_data_hash(self) -> str:
        return sha256_json(self.payload)

    def compute_event_hash(self) -> str:
        return sha256_json(self.hash_input())

    # --- защита от изменений ---

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError("События реестра нельзя изменять.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("События реестра нельзя удалять.")
