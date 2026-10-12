"""
Автотесты реестра. Запуск:  python manage.py test registry

Проверяется главное, что обещает реестр: нумерация, порядок статусов,
хэши файлов, связность цепочки событий, защита записей от изменений
и обнаружение подделок.
"""

import datetime
import hashlib
import io
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .hashing import GENESIS_HASH, canonical_json, sha256_json
from .models import Document, Dombra, ImmutableRecordError, Organization, RegistryEvent
from .services import DocumentUpload, RegistryError, record_event, verify_dombra

EventType = RegistryEvent.EventType
Status = Dombra.Status
DocType = Document.DocType

MEDIA_ROOT = tempfile.mkdtemp(prefix="dombra-tests-")


def upload(name="photo.jpg", content=b"fake image bytes"):
    return SimpleUploadedFile(name, content)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class RegistryTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("admin", "admin@example.com", "pass")
        cls.workshop = Organization.objects.create(
            name="Мастерская «Шебер»", org_type=Organization.OrgType.WORKSHOP
        )
        cls.lab = Organization.objects.create(
            name="Акустическая лаборатория", org_type=Organization.OrgType.LABORATORY
        )
        cls.cert_body = Organization.objects.create(
            name="Орган сертификации", org_type=Organization.OrgType.CERTIFICATION_BODY
        )

    def make_dombra(self, **kwargs):
        defaults = dict(
            title="Домбра концертная",
            master_name="Иванов Иван Иванович",
            manufacturer=self.workshop,
            manufacture_year=2025,
            dombra_type=Dombra.DombraType.WESTERN,
            body_material="клён",
            frets_count=19,
            total_length_mm=1000,
            scale_length_mm=680,
            created_by=self.user,
        )
        defaults.update(kwargs)
        return Dombra.objects.create(**defaults)

    def event(self, dombra, event_type, **kwargs):
        kwargs.setdefault("description", "описание")
        return record_event(dombra=dombra, event_type=event_type, user=self.user, **kwargs)

    def certify(self, dombra, number="CERT-001"):
        """Провести домбру по полному пути до сертификата."""
        self.event(dombra, EventType.REGISTERED)
        self.event(dombra, EventType.SUBMITTED, organization=self.lab)
        self.event(dombra, EventType.TESTED, organization=self.lab)
        return self.event(
            dombra,
            EventType.CERTIFIED,
            organization=self.cert_body,
            certificate_number=number,
            certification_date=datetime.date(2026, 9, 1),
            uploads=[DocumentUpload(upload("cert.pdf", b"%PDF certificate"), DocType.CERTIFICATE)],
        )


class HashingTests(RegistryTestCase):
    def test_canonical_json_does_not_depend_on_key_order(self):
        first = {"б": 1, "a": [1, 2], "в": None}
        second = {"в": None, "a": [1, 2], "б": 1}
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(sha256_json(first), sha256_json(second))
        self.assertEqual(canonical_json({"ключ": "значение"}), '{"ключ":"значение"}'.encode())

    def test_document_hash_matches_hashlib(self):
        content = b"dombra sound" * 1000
        dombra = self.make_dombra()
        document = Document.objects.create(
            dombra=dombra, doc_type=DocType.AUDIO, file=upload("sound.wav", content)
        )
        self.assertEqual(document.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(document.size, len(content))
        self.assertEqual(document.original_name, "sound.wav")
        self.assertEqual(document.compute_file_hash(), document.sha256)


class RegistrationTests(RegistryTestCase):
    def test_numbers_are_sequential(self):
        year = timezone.localdate().year
        first, second = self.make_dombra(), self.make_dombra(title="Вторая")
        self.event(first, EventType.REGISTERED)
        self.event(second, EventType.REGISTERED)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.registration_number, f"DOM-KZ-{year}-000001")
        self.assertEqual(second.registration_number, f"DOM-KZ-{year}-000002")
        self.assertEqual(first.status, Status.REGISTERED)
        self.assertIsNotNone(first.registered_at)

    def test_draft_has_no_number(self):
        self.assertIsNone(self.make_dombra().registration_number)

    def test_registration_covers_draft_documents(self):
        dombra = self.make_dombra()
        photo = Document.objects.create(dombra=dombra, doc_type=DocType.PHOTO, file=upload())
        event = self.event(dombra, EventType.REGISTERED)
        photo.refresh_from_db()
        self.assertEqual(photo.event, event)
        self.assertEqual(event.document_hashes(), [photo.sha256])
        self.assertEqual(event.payload["passport"]["title"], "Домбра концертная")
        self.assertEqual(event.payload["passport"]["registration_number"], dombra_number(dombra))


def dombra_number(dombra):
    dombra.refresh_from_db()
    return dombra.registration_number


class LifecycleTests(RegistryTestCase):
    def test_full_path_to_certificate(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        dombra.refresh_from_db()
        self.assertEqual(dombra.status, Status.CERTIFIED)
        self.assertEqual(dombra.certificate_number, "CERT-001")
        self.assertEqual(dombra.certified_by, self.cert_body)
        self.assertEqual(dombra.certification_status, "Сертифицирована")
        self.assertEqual(dombra.events.count(), 4)

    def test_wrong_order_is_rejected(self):
        dombra = self.make_dombra()
        with self.assertRaises(RegistryError):
            self.event(dombra, EventType.TESTED, organization=self.lab)
        self.event(dombra, EventType.REGISTERED)
        with self.assertRaises(RegistryError):
            self.event(dombra, EventType.REGISTERED)
        with self.assertRaises(RegistryError):
            self.event(
                dombra,
                EventType.CERTIFIED,
                organization=self.cert_body,
                certificate_number="X",
                certification_date=datetime.date(2026, 1, 1),
            )
        self.assertEqual(dombra.events.count(), 1)

    def test_required_data(self):
        dombra = self.make_dombra()
        self.event(dombra, EventType.REGISTERED)
        with self.assertRaises(RegistryError):  # ремонт без описания
            self.event(dombra, EventType.REPAIR, description="  ")
        with self.assertRaises(RegistryError):  # «добавить документ» без файла
            self.event(dombra, EventType.DOCUMENT_ADDED)
        self.event(dombra, EventType.SUBMITTED)
        with self.assertRaises(RegistryError):  # испытание без лаборатории
            self.event(dombra, EventType.TESTED)

    def test_repair_keeps_status_and_recheck_cycle(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        repair = self.event(dombra, EventType.REPAIR, description="Замена струн")
        self.assertEqual((repair.status_from, repair.status_to), (Status.CERTIFIED, Status.CERTIFIED))
        self.event(dombra, EventType.RECHECK_REQUIRED, description="После ремонта")
        self.event(dombra, EventType.SUBMITTED, organization=self.lab)
        self.event(dombra, EventType.TESTED, organization=self.lab)
        dombra.refresh_from_db()
        self.assertEqual(dombra.status, Status.TESTED)
        self.assertTrue(verify_dombra(dombra).ok)

    def test_certificate_number_is_unique(self):
        first, second = self.make_dombra(), self.make_dombra(title="Вторая")
        self.certify(first, number="CERT-777")
        with self.assertRaises(RegistryError):
            self.certify(second, number="CERT-777")

    def test_wrong_file_type_rolls_everything_back(self):
        dombra = self.make_dombra()
        self.event(dombra, EventType.REGISTERED)
        with self.assertRaises(RegistryError):
            self.event(
                dombra,
                EventType.DOCUMENT_ADDED,
                uploads=[DocumentUpload(upload("virus.exe", b"MZ"), DocType.PHOTO)],
            )
        self.assertEqual(dombra.events.count(), 1)
        self.assertEqual(dombra.documents.count(), 0)

    def test_failed_event_leaves_no_files(self):
        dombra = self.make_dombra()
        self.certify(dombra, number="CERT-555")
        other = self.make_dombra(title="Вторая")
        self.event(other, EventType.REGISTERED)
        self.event(other, EventType.SUBMITTED)
        self.event(other, EventType.TESTED, organization=self.lab)
        folder = Path(MEDIA_ROOT) / "dombras" / str(other.pk)
        with self.assertRaises(RegistryError):  # номер сертификата уже занят
            self.event(
                other,
                EventType.CERTIFIED,
                organization=self.cert_body,
                certificate_number="CERT-555",
                certification_date=datetime.date(2026, 9, 1),
                uploads=[DocumentUpload(upload("cert2.pdf", b"%PDF 2"), DocType.CERTIFICATE)],
            )
        leftovers = [path for path in folder.rglob("*") if path.is_file()] if folder.exists() else []
        self.assertEqual(leftovers, [])


class ChainTests(RegistryTestCase):
    def test_chain_links(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        events = list(dombra.events.order_by("sequence"))
        self.assertEqual([e.sequence for e in events], [1, 2, 3, 4])
        self.assertEqual(events[0].previous_hash, GENESIS_HASH)
        for previous, current in zip(events, events[1:]):
            self.assertEqual(current.previous_hash, previous.event_hash)
            self.assertEqual(current.status_from, previous.status_to)
        for event in events:
            self.assertEqual(event.compute_event_hash(), event.event_hash)
            self.assertEqual(event.compute_data_hash(), event.data_hash)
        result = verify_dombra(dombra)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual((result.events_checked, result.documents_checked), (4, 1))

    def test_hashes_survive_reload_from_database(self):
        dombra = self.make_dombra(manufacture_date=datetime.date(2025, 5, 17))
        event = self.event(dombra, EventType.REGISTERED, description="Первичная регистрация")
        reloaded = RegistryEvent.objects.get(pk=event.pk)
        self.assertEqual(reloaded.compute_event_hash(), event.event_hash)
        self.assertEqual(reloaded.compute_data_hash(), event.data_hash)

    def test_hashes_survive_dumpdata_and_loaddata(self):
        """Перенос базы (SQLite → PostgreSQL) через выгрузку в JSON не должен ломать хэши."""
        dombra = self.make_dombra()
        self.certify(dombra)
        for event in dombra.events.all():
            dumped = serializers.serialize("json", [event])
            restored = next(serializers.deserialize("json", dumped)).object
            self.assertEqual(restored.created_at, event.created_at)
            self.assertEqual(restored.compute_data_hash(), event.data_hash)
            self.assertEqual(restored.compute_event_hash(), event.event_hash)

    def test_events_are_immutable(self):
        dombra = self.make_dombra()
        event = self.event(dombra, EventType.REGISTERED)
        event.description = "исправлено задним числом"
        with self.assertRaises(ImmutableRecordError):
            event.save()
        with self.assertRaises(ImmutableRecordError):
            event.delete()
        with self.assertRaises(ImmutableRecordError):
            RegistryEvent.objects.filter(pk=event.pk).update(description="x")
        with self.assertRaises(ImmutableRecordError):
            RegistryEvent.objects.all().delete()

    def test_passport_is_frozen_after_registration(self):
        dombra = self.make_dombra()
        dombra.title = "Черновик можно править"
        dombra.save()
        self.event(dombra, EventType.REGISTERED)
        dombra.refresh_from_db()
        dombra.master_name = "Другой мастер"
        with self.assertRaises(ImmutableRecordError):
            dombra.save()
        dombra.refresh_from_db()
        dombra.is_public = True  # публикация паспортом не считается
        dombra.save()

    def test_recorded_documents_are_locked(self):
        dombra = self.make_dombra()
        document = Document.objects.create(dombra=dombra, doc_type=DocType.PHOTO, file=upload())
        self.event(dombra, EventType.REGISTERED)
        document.refresh_from_db()
        with self.assertRaises(ImmutableRecordError):
            document.delete()
        with self.assertRaises(ImmutableRecordError):
            Document.objects.filter(pk=document.pk).delete()
        document.sha256 = "0" * 64
        with self.assertRaises(ImmutableRecordError):
            document.save()
        document.refresh_from_db()
        document.title = "Вид спереди"  # описание менять можно
        document.save()


class TamperingTests(RegistryTestCase):
    """Подделки делаются в обход Django, прямыми SQL-запросами, как это сделал бы злоумышленник."""

    def raw(self, sql, params):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def test_edited_event_is_detected(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        target = dombra.events.get(sequence=2)
        self.raw("UPDATE registry_registryevent SET description = %s WHERE id = %s", ["подмена", target.pk])
        result = verify_dombra(dombra)
        self.assertFalse(result.ok)
        self.assertTrue(any("№2" in problem for problem in result.problems), result.problems)

    def test_recomputed_event_breaks_the_next_link(self):
        """Даже если подделать запись вместе с её хэшем, разорвётся связь со следующим событием."""
        dombra = self.make_dombra()
        self.certify(dombra)
        target = dombra.events.get(sequence=2)
        target.description = "подмена"
        forged_hash = target.compute_event_hash()
        self.raw(
            "UPDATE registry_registryevent SET description = %s, event_hash = %s WHERE id = %s",
            ["подмена", forged_hash, target.pk],
        )
        result = verify_dombra(dombra)
        self.assertFalse(result.ok)
        self.assertTrue(any("№3" in problem and "цепочка" in problem for problem in result.problems))

    def test_deleted_event_is_detected(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        self.event(dombra, EventType.REPAIR, description="Ремонт")
        target = dombra.events.get(sequence=5)
        self.raw("DELETE FROM registry_registryevent WHERE id = %s", [target.pk])
        self.event(dombra, EventType.INSPECTION, description="Осмотр")  # станет №5 поверх удалённого
        middle = dombra.events.get(sequence=3)
        self.raw("DELETE FROM registry_registryevent WHERE id = %s", [middle.pk])
        self.assertFalse(verify_dombra(dombra).ok)

    def test_changed_status_and_passport_are_detected(self):
        dombra = self.make_dombra()
        self.event(dombra, EventType.REGISTERED)
        self.raw(
            "UPDATE registry_dombra SET status = %s, master_name = %s WHERE id = %s",
            [Status.CERTIFIED, "Подставной мастер", dombra.pk.hex if connection.vendor == "sqlite" else dombra.pk],
        )
        dombra.refresh_from_db()
        problems = verify_dombra(dombra).problems
        self.assertTrue(any("Статус домбры" in problem for problem in problems), problems)
        self.assertTrue(any("ФИО мастера" in problem for problem in problems), problems)

    def test_changed_file_is_detected(self):
        dombra = self.make_dombra()
        document = Document.objects.create(dombra=dombra, doc_type=DocType.PHOTO, file=upload())
        self.event(dombra, EventType.REGISTERED)
        self.assertTrue(verify_dombra(dombra).ok)
        path = Path(document.file.path)
        path.write_bytes(path.read_bytes() + b"!")  # изменён один байт
        result = verify_dombra(dombra)
        self.assertFalse(result.ok)
        self.assertTrue(any("файл изменён" in problem for problem in result.problems))
        self.assertTrue(verify_dombra(dombra, check_files=False).ok)  # цепочка при этом цела
        path.unlink()
        self.assertTrue(any("отсутствует" in problem for problem in verify_dombra(dombra).problems))

    def test_changed_document_hash_is_detected(self):
        dombra = self.make_dombra()
        document = Document.objects.create(dombra=dombra, doc_type=DocType.PHOTO, file=upload())
        self.event(dombra, EventType.REGISTERED)
        self.raw("UPDATE registry_document SET sha256 = %s WHERE id = %s", ["f" * 64, document.pk])
        self.assertFalse(verify_dombra(dombra, check_files=False).ok)

    def test_management_command(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        out = io.StringIO()
        call_command("verify_registry", stdout=out)
        self.assertIn("Нарушений нет", out.getvalue())
        target = dombra.events.get(sequence=1)
        self.raw("UPDATE registry_registryevent SET description = %s WHERE id = %s", ["подмена", target.pk])
        with self.assertRaises(SystemExit):
            call_command("verify_registry", "--no-files", stdout=io.StringIO())


class AdminTests(RegistryTestCase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_pages_open(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        event = dombra.events.first()
        document = dombra.documents.first()
        urls = [
            reverse("admin:index"),
            reverse("admin:registry_dombra_changelist"),
            reverse("admin:registry_dombra_add"),
            reverse("admin:registry_dombra_change", args=[dombra.pk]),
            reverse("admin:registry_dombra_change", args=[self.make_dombra().pk]),
            reverse("admin:registry_document_changelist"),
            reverse("admin:registry_document_change", args=[document.pk]),
            reverse("admin:registry_registryevent_changelist"),
            reverse("admin:registry_registryevent_change", args=[event.pk]),
            reverse("admin:registry_organization_changelist"),
            reverse("admin:registry_dombra_event", args=[dombra.pk, EventType.REPAIR]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_draft_with_document_through_admin(self):
        data = {
            "title": "Домбра из админки",
            "master_name": "Мастер",
            "dombra_type": Dombra.DombraType.EASTERN,
            "manufacture_year": "2024",
            "documents-TOTAL_FORMS": "1",
            "documents-INITIAL_FORMS": "0",
            "documents-MIN_NUM_FORMS": "0",
            "documents-MAX_NUM_FORMS": "1000",
            "documents-0-doc_type": DocType.PHOTO,
            "documents-0-title": "Общий вид",
            "documents-0-file": upload("front.png", b"png bytes"),
            "documents-0-is_public": "on",
            "events-TOTAL_FORMS": "0",
            "events-INITIAL_FORMS": "0",
            "events-MIN_NUM_FORMS": "0",
            "events-MAX_NUM_FORMS": "0",
            "_save": "Сохранить",
        }
        response = self.client.post(reverse("admin:registry_dombra_add"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["errors"])
        dombra = Dombra.objects.get(title="Домбра из админки")
        self.assertEqual(dombra.created_by, self.user)
        document = dombra.documents.get()
        self.assertEqual(document.uploaded_by, self.user)
        self.assertEqual(document.sha256, hashlib.sha256(b"png bytes").hexdigest())

    def test_action_buttons_follow_status(self):
        dombra = self.make_dombra()
        page = self.client.get(reverse("admin:registry_dombra_change", args=[dombra.pk]))
        self.assertContains(page, "Зарегистрировать")
        self.assertNotContains(page, "Выдать сертификат")
        self.event(dombra, EventType.REGISTERED)
        page = self.client.get(reverse("admin:registry_dombra_change", args=[dombra.pk]))
        self.assertNotContains(page, "Зарегистрировать")
        self.assertContains(page, "Передать на проверку")
        self.assertContains(page, "Цепочка событий цела")

    def test_register_and_add_document_through_admin(self):
        dombra = self.make_dombra()
        url = reverse("admin:registry_dombra_event", args=[dombra.pk, EventType.REGISTERED])
        response = self.client.post(url, {"description": ""})
        self.assertRedirects(response, reverse("admin:registry_dombra_change", args=[dombra.pk]))
        dombra.refresh_from_db()
        self.assertEqual(dombra.status, Status.REGISTERED)

        url = reverse("admin:registry_dombra_event", args=[dombra.pk, EventType.DOCUMENT_ADDED])
        response = self.client.post(url, {"doc_type": DocType.AUDIO, "file": upload("kui.mp3", b"audio")})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(dombra.documents.get().event.event_type, EventType.DOCUMENT_ADDED)

        response = self.client.post(url, {"doc_type": DocType.AUDIO, "file": upload("kui.txt", b"text")})
        self.assertEqual(response.status_code, 200)  # форма с ошибкой, событие не создано
        self.assertEqual(dombra.events.count(), 2)

    def test_certify_through_admin(self):
        dombra = self.make_dombra()
        self.event(dombra, EventType.REGISTERED)
        self.event(dombra, EventType.SUBMITTED)
        self.event(dombra, EventType.TESTED, organization=self.lab)
        url = reverse("admin:registry_dombra_event", args=[dombra.pk, EventType.CERTIFIED])
        response = self.client.post(
            url,
            {
                "organization": self.cert_body.pk,
                "certificate_number": "KZ-0001",
                "certification_date": "2026-09-10",
                "doc_type": DocType.CERTIFICATE,
                "file": upload("certificate.pdf", b"%PDF"),
                "doc_is_public": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        dombra.refresh_from_db()
        self.assertEqual(dombra.status, Status.CERTIFIED)
        self.assertEqual(dombra.certification_date, datetime.date(2026, 9, 10))
        self.assertTrue(verify_dombra(dombra).ok)

    def test_unavailable_action_redirects_back(self):
        dombra = self.make_dombra()
        url = reverse("admin:registry_dombra_event", args=[dombra.pk, EventType.CERTIFIED])
        self.assertRedirects(self.client.get(url), reverse("admin:registry_dombra_change", args=[dombra.pk]))

    def test_registered_passport_is_read_only_in_admin(self):
        dombra = self.make_dombra()
        self.event(dombra, EventType.REGISTERED)
        page = self.client.get(reverse("admin:registry_dombra_change", args=[dombra.pk]))
        self.assertNotContains(page, 'name="master_name"')
        self.assertContains(page, 'name="is_public"')
        delete_url = reverse("admin:registry_dombra_delete", args=[dombra.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)

    def test_delete_draft_with_document_removes_file(self):
        dombra = self.make_dombra()
        document = Document.objects.create(dombra=dombra, doc_type=DocType.PHOTO, file=upload())
        stored = Path(document.file.path)
        self.assertTrue(stored.exists())
        url = reverse("admin:registry_dombra_delete", args=[dombra.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Dombra.objects.filter(pk=dombra.pk).exists())
        self.assertFalse(stored.exists())

    def test_full_verification_view(self):
        dombra = self.make_dombra()
        self.certify(dombra)
        url = reverse("admin:registry_dombra_verify", args=[dombra.pk])
        response = self.client.get(url, follow=True)
        self.assertContains(response, "Целостность подтверждена")
