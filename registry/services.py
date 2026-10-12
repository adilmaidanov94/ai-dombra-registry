"""
Бизнес-логика реестра: запись событий и проверка целостности.

Единственный правильный способ изменить статус домбры или добавить запись
в её историю — вызвать record_event(). Функция в одной транзакции:
  1) проверяет, что действие разрешено для текущего статуса;
  2) сохраняет загруженные файлы и считает их SHA-256;
  3) создаёт событие и связывает его хэшем с предыдущим событием;
  4) обновляет статус домбры и сведения о сертификате.

verify_dombra() заново пересчитывает все хэши и сообщает о расхождениях.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone

from .hashing import GENESIS_HASH, HASH_VERSION
from .models import Document, Dombra, RegistrationCounter, RegistryEvent, registry_now

Status = Dombra.Status
EventType = RegistryEvent.EventType
DocType = Document.DocType


class RegistryError(Exception):
    """Действие с реестром отклонено (неверный статус, не хватает данных и т. п.)."""


# ---------------------------------------------------------------------------
# Правила жизненного цикла
# ---------------------------------------------------------------------------

NON_DRAFT = frozenset(set(Status.values) - {Status.DRAFT})
STATUS_LABELS = dict(Status.choices)


@dataclass(frozen=True)
class EventRule:
    """Правило для одного типа события."""

    from_statuses: frozenset      # из каких статусов действие разрешено
    to_status: str | None         # новый статус (None — статус не меняется)
    button_label: str             # подпись кнопки в панели администратора
    description_required: bool = False
    organization_required: bool = False
    file_required: bool = False
    allows_upload: bool = True
    default_doc_type: str = DocType.OTHER
    hint: str = ""


EVENT_RULES = {
    EventType.REGISTERED: EventRule(
        from_statuses=frozenset({Status.DRAFT}),
        to_status=Status.REGISTERED,
        button_label="Зарегистрировать",
        allows_upload=False,
        hint=(
            "Домбре будет присвоен регистрационный номер. Паспорт и все уже "
            "загруженные документы войдут в хэш события, после этого изменить "
            "их будет нельзя."
        ),
    ),
    EventType.SUBMITTED: EventRule(
        from_statuses=frozenset({Status.REGISTERED, Status.RECHECK_REQUIRED}),
        to_status=Status.SUBMITTED,
        button_label="Передать на проверку",
        hint="Укажите лабораторию, в которую передан инструмент.",
    ),
    EventType.INSPECTION: EventRule(
        from_statuses=NON_DRAFT,
        to_status=None,
        button_label="Записать проверку",
        description_required=True,
        default_doc_type=DocType.PROTOCOL,
        hint="Осмотр или измерения без изменения статуса.",
    ),
    EventType.TESTED: EventRule(
        from_statuses=frozenset({Status.SUBMITTED}),
        to_status=Status.TESTED,
        button_label="Провести испытание",
        description_required=True,
        organization_required=True,
        default_doc_type=DocType.PROTOCOL,
        hint="Опишите результаты испытания, приложите протокол или аудиозапись.",
    ),
    EventType.CERTIFIED: EventRule(
        from_statuses=frozenset({Status.TESTED}),
        to_status=Status.CERTIFIED,
        button_label="Выдать сертификат",
        organization_required=True,
        default_doc_type=DocType.CERTIFICATE,
        hint="Приложите файл сертификата: его хэш будет показан в публичном паспорте.",
    ),
    EventType.SUSPENDED: EventRule(
        from_statuses=frozenset({Status.CERTIFIED}),
        to_status=Status.SUSPENDED,
        button_label="Приостановить сертификат",
        description_required=True,
        hint="Укажите причину приостановки.",
    ),
    EventType.RECHECK_REQUIRED: EventRule(
        from_statuses=frozenset({Status.TESTED, Status.CERTIFIED, Status.SUSPENDED}),
        to_status=Status.RECHECK_REQUIRED,
        button_label="Отправить на повторную проверку",
        description_required=True,
        hint="Укажите причину повторной проверки.",
    ),
    EventType.REPAIR: EventRule(
        from_statuses=NON_DRAFT,
        to_status=None,
        button_label="Добавить ремонт",
        description_required=True,
        default_doc_type=DocType.REPAIR_ACT,
        hint="Опишите выполненные работы. Статус при этом не меняется.",
    ),
    EventType.DOCUMENT_ADDED: EventRule(
        from_statuses=NON_DRAFT,
        to_status=None,
        button_label="Добавить документ",
        file_required=True,
        default_doc_type=DocType.PHOTO,
        hint="Файл будет записан в историю вместе со своим хэшем SHA-256.",
    ),
}


def available_event_types(dombra: Dombra) -> list:
    """Какие действия доступны для домбры в её текущем статусе."""
    return [
        event_type
        for event_type, rule in EVENT_RULES.items()
        if dombra.status in rule.from_statuses
    ]


# ---------------------------------------------------------------------------
# Запись события
# ---------------------------------------------------------------------------


@dataclass
class DocumentUpload:
    """Файл, прикладываемый к событию."""

    file: object
    doc_type: str
    title: str = ""
    is_public: bool = False


def format_registration_number(year: int, number: int) -> str:
    prefix = getattr(settings, "REGISTRY_NUMBER_PREFIX", "DOM-KZ")
    return f"{prefix}-{year}-{number:06d}"


def _validation_message(error: ValidationError) -> str:
    return " ".join(error.messages)


def record_event(**kwargs) -> RegistryEvent:
    """
    Записать событие в историю домбры. Возвращает созданное событие.

    Параметры: dombra, event_type, user, description="", organization=None,
    uploads=(), certificate_number="", certification_date=None.
    Если что-то пошло не так, транзакция откатывается, а уже сохранённые
    на диск файлы удаляются.
    """
    saved_files = []
    try:
        with transaction.atomic():
            event = _record_event(saved_files=saved_files, **kwargs)
    except Exception:
        for storage, name in saved_files:
            try:
                storage.delete(name)
            except OSError:
                pass
        raise
    # Объект домбры у вызывающего кода получает новый статус и номер.
    kwargs["dombra"].refresh_from_db()
    return event


def _record_event(
    *,
    dombra: Dombra,
    event_type: str,
    user,
    description: str = "",
    organization=None,
    uploads=(),
    certificate_number: str = "",
    certification_date=None,
    saved_files: list,
) -> RegistryEvent:
    rule = EVENT_RULES.get(event_type)
    if rule is None:
        raise RegistryError(f"Неизвестный тип события: {event_type}.")
    if user is None or not getattr(user, "pk", None):
        raise RegistryError("У события должен быть автор.")

    # Блокируем строку домбры: два события одной домбры не запишутся одновременно.
    dombra = Dombra.objects.select_for_update().get(pk=dombra.pk)
    description = (description or "").strip()
    uploads = list(uploads)

    # 1. Проверки.
    if dombra.status not in rule.from_statuses:
        raise RegistryError(
            f"Действие «{rule.button_label}» недоступно в статусе "
            f"«{dombra.get_status_display()}»."
        )
    if rule.description_required and not description:
        raise RegistryError("Для этого события нужно описание.")
    if rule.organization_required and organization is None:
        raise RegistryError("Для этого события нужно указать организацию.")
    if uploads and not rule.allows_upload:
        raise RegistryError("К этому событию файлы не прикладываются.")
    if rule.file_required and not uploads:
        raise RegistryError("Для этого события нужно приложить файл.")

    certificate_number = (certificate_number or "").strip()
    if event_type == EventType.CERTIFIED:
        if not certificate_number or certification_date is None:
            raise RegistryError("Укажите номер и дату сертификата.")
        if (
            Dombra.objects.filter(certificate_number=certificate_number)
            .exclude(pk=dombra.pk)
            .exists()
        ):
            raise RegistryError(f"Сертификат № {certificate_number} уже выдан другой домбре.")

    now = registry_now()
    status_from = dombra.status
    status_to = rule.to_status or dombra.status
    payload = {}

    # 2. Регистрация: номер и снимок паспорта.
    if event_type == EventType.REGISTERED:
        try:
            dombra.full_clean()
        except ValidationError as error:
            raise RegistryError(
                f"Паспорт заполнен с ошибками: {_validation_message(error)}"
            ) from error
        year = timezone.localtime(now).year
        dombra.registration_number = format_registration_number(
            year, RegistrationCounter.next_value(year)
        )
        dombra.registered_at = now
        payload["passport"] = dombra.passport_snapshot()

    # 3. Документы: новые файлы + (при регистрации) всё, что загружено в черновик.
    documents = []
    if event_type == EventType.REGISTERED:
        documents.extend(dombra.documents.filter(event__isnull=True).order_by("id"))
    for upload in uploads:
        document = Document(
            dombra=dombra,
            doc_type=upload.doc_type,
            title=upload.title,
            file=upload.file,
            is_public=upload.is_public,
            uploaded_by=user,
        )
        try:
            document.full_clean(exclude=["original_name", "sha256"])
        except ValidationError as error:
            raise RegistryError(_validation_message(error)) from error
        document.save()
        saved_files.append((document.file.storage, document.file.name))
        documents.append(document)
    payload["documents"] = [document.manifest() for document in documents]

    # 4. Сведения о сертификате.
    if event_type == EventType.CERTIFIED:
        payload["certificate"] = {
            "number": certificate_number,
            "date": certification_date.isoformat(),
            "organization_id": organization.pk,
            "organization_name": organization.name,
        }
    if organization is not None:
        payload["organization_name"] = organization.name

    # 5. Звено цепочки.
    last_event = dombra.events.order_by("-sequence").first()
    event = RegistryEvent(
        dombra=dombra,
        sequence=(last_event.sequence + 1) if last_event else 1,
        event_type=event_type,
        created_at=now,
        created_by=user,
        created_by_name=user.get_username()[:150],
        organization=organization,
        description=description,
        status_from=status_from,
        status_to=status_to,
        payload=payload,
        hash_version=HASH_VERSION,
        previous_hash=last_event.event_hash if last_event else GENESIS_HASH,
    )
    event.data_hash = event.compute_data_hash()
    event.event_hash = event.compute_event_hash()
    event.save()

    # 6. Документы привязываются к событию и с этого момента защищены.
    if documents:
        Document.objects.filter(pk__in=[document.pk for document in documents]).update(
            event=event
        )

    # 7. Состояние домбры.
    update_fields = ["status", "updated_at"]
    dombra.status = status_to
    if event_type == EventType.REGISTERED:
        update_fields += ["registration_number", "registered_at", "manufacture_year"]
    if event_type == EventType.CERTIFIED:
        dombra.certificate_number = certificate_number
        dombra.certification_date = certification_date
        dombra.certified_by = organization
        update_fields += ["certificate_number", "certification_date", "certified_by"]
    dombra.save(update_fields=update_fields)
    return event


# ---------------------------------------------------------------------------
# Проверка целостности
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    dombra: Dombra
    events_checked: int = 0
    documents_checked: int = 0
    files_checked: bool = False
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        scope = f"событий: {self.events_checked}, документов: {self.documents_checked}"
        if not self.files_checked:
            scope += " (файлы не пересчитывались)"
        if self.ok:
            return f"Целостность подтверждена ({scope})."
        return f"Найдено нарушений: {len(self.problems)} ({scope})."


def _field_label(name: str) -> str:
    try:
        return str(Dombra._meta.get_field(name).verbose_name)
    except FieldDoesNotExist:  # поле вида manufacturer_id
        return str(Dombra._meta.get_field(name.removesuffix("_id")).verbose_name)


def verify_dombra(dombra: Dombra, *, check_files: bool = True) -> VerificationResult:
    """Пересчитать цепочку событий домбры и (по желанию) хэши файлов."""
    # Проверяется то, что лежит в базе, а не объект в памяти: он мог устареть.
    dombra = Dombra.objects.select_related("manufacturer").get(pk=dombra.pk)
    result = VerificationResult(dombra=dombra, files_checked=check_files)
    problems = result.problems
    events = list(dombra.events.order_by("sequence"))
    result.events_checked = len(events)

    # --- цепочка событий ---
    expected_previous = GENESIS_HASH
    expected_status = Status.DRAFT
    for position, event in enumerate(events, start=1):
        label = f"Событие №{event.sequence} «{event.get_event_type_display()}»"
        if event.sequence != position:
            problems.append(
                f"{label}: нарушена нумерация, ожидался №{position}. "
                "Возможно, событие удалено."
            )
        if event.previous_hash != expected_previous:
            problems.append(f"{label}: не совпадает хэш предыдущего события, цепочка разорвана.")
        if event.compute_data_hash() != event.data_hash:
            problems.append(f"{label}: данные события изменены (хэш данных не совпадает).")
        if event.compute_event_hash() != event.event_hash:
            problems.append(f"{label}: запись изменена (хэш события не совпадает).")
        if event.status_from != expected_status:
            problems.append(f"{label}: статус «до» не продолжает предыдущее событие.")
        expected_previous = event.event_hash
        expected_status = event.status_to

    # --- текущее состояние домбры против истории ---
    if dombra.status != expected_status:
        problems.append(
            f"Статус домбры «{dombra.get_status_display()}» не соответствует истории: "
            f"по событиям должен быть «{STATUS_LABELS.get(expected_status, expected_status)}»."
        )

    registration = next((e for e in events if e.event_type == EventType.REGISTERED), None)
    if registration is not None:
        recorded = registration.payload.get("passport", {})
        current = dombra.passport_snapshot()
        for key, value in current.items():
            if key == "manufacturer_name":
                continue  # организацию могли переименовать, это не подделка паспорта
            if recorded.get(key) != value:
                problems.append(
                    f"Паспорт: поле «{_field_label(key)}» изменено после регистрации."
                )
    elif not dombra.is_draft:
        problems.append("У домбры нет события регистрации.")

    certification = next(
        (e for e in reversed(events) if e.event_type == EventType.CERTIFIED), None
    )
    recorded_certificate = certification.payload.get("certificate", {}) if certification else {}
    current_certificate = {
        "number": dombra.certificate_number or None,
        "date": dombra.certification_date.isoformat() if dombra.certification_date else None,
        "organization_id": dombra.certified_by_id,
    }
    for key, value in current_certificate.items():
        if recorded_certificate.get(key) != value:
            problems.append("Сведения о сертификате не соответствуют истории событий.")
            break

    # --- документы ---
    documents = list(dombra.documents.all())
    result.documents_checked = len(documents)
    by_event = {}
    for document in documents:
        by_event.setdefault(document.event_id, []).append(document)

    for event in events:
        recorded_hashes = sorted(event.document_hashes())
        actual_hashes = sorted(d.sha256 for d in by_event.get(event.pk, []))
        if recorded_hashes != actual_hashes:
            problems.append(
                f"Событие №{event.sequence}: состав документов не совпадает с записанным "
                "(документ удалён, добавлен в обход реестра или изменён его хэш)."
            )
    if not dombra.is_draft:
        for document in by_event.get(None, []):
            problems.append(f"Документ «{document.original_name}» не записан ни в одно событие.")

    if check_files:
        for document in documents:
            try:
                actual = document.compute_file_hash()
            except FileNotFoundError:
                problems.append(f"Документ «{document.original_name}»: файл отсутствует в хранилище.")
                continue
            except OSError as error:
                problems.append(f"Документ «{document.original_name}»: файл не читается ({error}).")
                continue
            if actual != document.sha256:
                problems.append(
                    f"Документ «{document.original_name}»: файл изменён, SHA-256 не совпадает."
                )
    return result


def verify_registry(*, check_files: bool = True):
    """Проверить все домбры реестра. Возвращает генератор результатов."""
    for dombra in Dombra.objects.order_by("registration_number", "created_at").iterator():
        yield verify_dombra(dombra, check_files=check_files)
