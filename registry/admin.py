"""
Панель администратора реестра домбр.

Черновик паспорта редактируется как обычная запись. Всё, что происходит
после регистрации, выполняется кнопками действий на странице домбры:
каждое действие создаёт событие в цепочке хэшей (см. registry/services.py).
"""

import json

from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.template.defaultfilters import filesizeformat
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services
from .forms import EventActionForm
from .models import Document, Dombra, Organization, RegistryEvent

admin.site.site_header = "Реестр домбр"
admin.site.site_title = "Реестр домбр"
admin.site.index_title = "Управление реестром"

STATUS_COLORS = {
    Dombra.Status.DRAFT: "#6b7280",
    Dombra.Status.REGISTERED: "#2563eb",
    Dombra.Status.SUBMITTED: "#d97706",
    Dombra.Status.TESTED: "#7c3aed",
    Dombra.Status.CERTIFIED: "#15803d",
    Dombra.Status.SUSPENDED: "#b91c1c",
    Dombra.Status.RECHECK_REQUIRED: "#b45309",
}


def short_hash(value: str):
    """Первые символы хэша; полный хэш виден во всплывающей подсказке."""
    if not value:
        return "—"
    return format_html('<code title="{}">{}…</code>', value, value[:12])


def status_badge(status: str, label: str):
    return format_html(
        '<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
        'background:{};color:#fff;font-size:12px;white-space:nowrap;">{}</span>',
        STATUS_COLORS.get(status, "#6b7280"),
        label,
    )


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "org_type", "city", "accreditation_number", "is_active")
    list_filter = ("org_type", "is_active")
    search_fields = ("name", "city", "accreditation_number")


# ---------------------------------------------------------------------------
# Встроенные таблицы на странице домбры
# ---------------------------------------------------------------------------


class DocumentInline(admin.TabularInline):
    model = Document
    extra = 0
    fields = (
        "doc_type",
        "title",
        "file",
        "is_public",
        "hash_display",
        "size_display",
        "event_display",
    )
    readonly_fields = ("hash_display", "size_display", "event_display")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and not obj.is_draft:
            # После регистрации файлы добавляются только кнопкой «Добавить документ».
            readonly += ["doc_type", "file"]
        return readonly

    def has_add_permission(self, request, obj=None):
        return obj is None or obj.is_draft

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.is_draft

    @admin.display(description="SHA-256")
    def hash_display(self, document):
        return short_hash(document.sha256)

    @admin.display(description="Размер")
    def size_display(self, document):
        return filesizeformat(document.size) if document.pk else "—"

    @admin.display(description="Событие")
    def event_display(self, document):
        if not document.event_id:
            return "не записан"
        return f"№{document.event.sequence}"


class RegistryEventInline(admin.TabularInline):
    model = RegistryEvent
    extra = 0
    can_delete = False
    show_change_link = True
    ordering = ("sequence",)
    verbose_name_plural = "История событий"
    fields = (
        "sequence",
        "when_display",
        "event_type",
        "status_to",
        "description",
        "organization",
        "author_display",
        "hash_display",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Дата и время")
    def when_display(self, event):
        local = timezone.localtime(event.created_at)
        return format_html(
            '<span style="white-space:nowrap;">{}</span>', local.strftime("%d.%m.%Y %H:%M")
        )

    @admin.display(description="Автор")
    def author_display(self, event):
        return event.created_by_name

    @admin.display(description="Хэш события")
    def hash_display(self, event):
        return short_hash(event.event_hash)


# ---------------------------------------------------------------------------
# Домбры
# ---------------------------------------------------------------------------


@admin.register(Dombra)
class DombraAdmin(admin.ModelAdmin):
    list_display = (
        "number_display",
        "title",
        "master_name",
        "dombra_type",
        "status_display",
        "public_display",
    )
    list_display_links = ("number_display", "title")
    list_filter = ("status", "dombra_type", "is_public")
    list_select_related = ("manufacturer",)
    search_fields = ("registration_number", "title", "master_name", "certificate_number")
    autocomplete_fields = ("manufacturer",)
    inlines = (DocumentInline, RegistryEventInline)
    actions = ("verify_selected",)
    save_on_top = True

    base_readonly_fields = (
        "registration_number",
        "status_display",
        "registered_at",
        "id",
        "certification_status_display",
        "certificate_number",
        "certification_date",
        "certified_by",
        "created_by",
        "created_at",
        "updated_at",
    )

    identification_fieldset = (
        "Идентификация",
        {"fields": ("registration_number", "status_display", "registered_at", "id")},
    )
    passport_fieldsets = (
        (
            "Паспорт",
            {
                "fields": (
                    "title",
                    "master_name",
                    "manufacturer",
                    ("manufacture_year", "manufacture_date"),
                    "manufacture_place",
                    "dombra_type",
                )
            },
        ),
        (
            "Характеристики",
            {
                "fields": (
                    ("body_material", "neck_material", "soundboard_material"),
                    "frets_count",
                    ("total_length_mm", "scale_length_mm"),
                    "strings_type",
                    "tuning",
                    "weight_g",
                )
            },
        ),
    )
    certification_fieldset = (
        "Сертификация",
        {
            "fields": (
                "certification_status_display",
                "certificate_number",
                "certification_date",
                "certified_by",
            )
        },
    )
    service_fieldset = (
        "Публикация и служебные сведения",
        {"fields": ("is_public", "internal_notes", "created_by", "created_at", "updated_at")},
    )

    # --- отображение ---

    @admin.display(description="Рег. номер", ordering="registration_number")
    def number_display(self, dombra):
        return format_html(
            '<span style="white-space:nowrap;">{}</span>',
            dombra.registration_number or "черновик",
        )

    @admin.display(description="Публ.", boolean=True, ordering="is_public")
    def public_display(self, dombra):
        return dombra.is_public

    @admin.display(description="Статус", ordering="status")
    def status_display(self, dombra):
        return status_badge(dombra.status, dombra.get_status_display())

    @admin.display(description="Статус сертификации")
    def certification_status_display(self, dombra):
        return dombra.certification_status

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (*self.passport_fieldsets, self.service_fieldset)
        return (
            self.identification_fieldset,
            *self.passport_fieldsets,
            self.certification_fieldset,
            self.service_fieldset,
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.base_readonly_fields)
        if obj is not None and not obj.is_draft:
            readonly += list(Dombra.PASSPORT_FIELDS)
        return readonly

    # --- права ---

    def has_delete_permission(self, request, obj=None):
        allowed = super().has_delete_permission(request, obj)
        if obj is not None and not obj.is_draft:
            return False  # зарегистрированные инструменты из реестра не удаляются
        return allowed

    # --- сохранение ---

    def save_model(self, request, obj, form, change):
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        if formset.model is not Document:
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        for deleted in formset.deleted_objects:
            deleted.delete()
        for document in instances:
            if document._state.adding:
                document.uploaded_by = request.user
            document.save()
        formset.save_m2m()

    # --- кнопки действий и проверка цепочки на странице домбры ---

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/event/<str:event_type>/",
                self.admin_site.admin_view(self.event_view),
                name="registry_dombra_event",
            ),
            path(
                "<path:object_id>/verify/",
                self.admin_site.admin_view(self.verify_view),
                name="registry_dombra_verify",
            ),
        ]
        return custom + super().get_urls()

    def _change_url(self, dombra):
        return reverse("admin:registry_dombra_change", args=[dombra.pk])

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = dict(extra_context or {})
        dombra = self.get_object(request, unquote(object_id))
        if dombra is not None:
            actions = []
            if self.has_change_permission(request, dombra):
                for event_type in services.available_event_types(dombra):
                    rule = services.EVENT_RULES[event_type]
                    actions.append(
                        {
                            "label": rule.button_label,
                            "primary": rule.to_status is not None,
                            "url": reverse(
                                "admin:registry_dombra_event", args=[dombra.pk, event_type]
                            ),
                        }
                    )
            extra_context.update(
                registry_actions=actions,
                registry_check=services.verify_dombra(dombra, check_files=False),
                registry_verify_url=reverse("admin:registry_dombra_verify", args=[dombra.pk]),
                registry_status_badge=status_badge(dombra.status, dombra.get_status_display()),
            )
        return super().change_view(request, object_id, form_url, extra_context)

    def _get_dombra_or_404(self, request, object_id):
        dombra = self.get_object(request, unquote(object_id))
        if dombra is None:
            raise Http404("Домбра не найдена.")
        if not self.has_change_permission(request, dombra):
            raise PermissionDenied
        return dombra

    def event_view(self, request, object_id, event_type):
        """Страница действия: форма → запись события в цепочку."""
        dombra = self._get_dombra_or_404(request, object_id)
        rule = services.EVENT_RULES.get(event_type)
        if rule is None:
            raise Http404("Неизвестное действие.")
        if dombra.status not in rule.from_statuses:
            self.message_user(
                request,
                f"Действие «{rule.button_label}» недоступно в статусе "
                f"«{dombra.get_status_display()}».",
                messages.ERROR,
            )
            return HttpResponseRedirect(self._change_url(dombra))

        if request.method == "POST":
            form = EventActionForm(request.POST, request.FILES, event_type=event_type)
        else:
            form = EventActionForm(event_type=event_type)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            try:
                event = services.record_event(
                    dombra=dombra,
                    event_type=event_type,
                    user=request.user,
                    description=data.get("description", ""),
                    organization=data.get("organization"),
                    uploads=form.uploads(),
                    certificate_number=data.get("certificate_number", ""),
                    certification_date=data.get("certification_date"),
                )
            except services.RegistryError as error:
                form.add_error(None, str(error))
            else:
                self.message_user(
                    request,
                    format_html(
                        "Событие «{}» записано под №{}. Хэш события: <code>{}</code>",
                        event.get_event_type_display(),
                        event.sequence,
                        event.event_hash,
                    ),
                    messages.SUCCESS,
                )
                return HttpResponseRedirect(self._change_url(dombra))

        admin_form = helpers.AdminForm(
            form, [(None, {"fields": list(form.fields)})], {}, model_admin=self
        )
        context = {
            **self.admin_site.each_context(request),
            "title": rule.button_label,
            "subtitle": str(dombra),
            "opts": self.model._meta,
            "original": dombra,
            "rule": rule,
            "form": form,
            "adminform": admin_form,
            "media": self.media + form.media,
            "change_url": self._change_url(dombra),
            "draft_documents": (
                dombra.documents.filter(event__isnull=True)
                if event_type == RegistryEvent.EventType.REGISTERED
                else None
            ),
        }
        return TemplateResponse(request, "admin/registry/dombra/event_form.html", context)

    def verify_view(self, request, object_id):
        """Полная проверка одной домбры: цепочка событий и хэши файлов."""
        dombra = self._get_dombra_or_404(request, object_id)
        self._report(request, services.verify_dombra(dombra, check_files=True))
        return HttpResponseRedirect(self._change_url(dombra))

    def _report(self, request, result):
        if result.ok:
            self.message_user(request, f"{result.dombra}: {result.summary()}", messages.SUCCESS)
            return
        details = format_html_join("", "<br>— {}", ((problem,) for problem in result.problems))
        self.message_user(
            request,
            format_html("{}: {}{}", str(result.dombra), result.summary(), details),
            messages.ERROR,
        )

    @admin.action(description="Проверить целостность (цепочка и файлы)")
    def verify_selected(self, request, queryset):
        for dombra in queryset:
            self._report(request, services.verify_dombra(dombra, check_files=True))


# ---------------------------------------------------------------------------
# Документы
# ---------------------------------------------------------------------------


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "original_name",
        "doc_type",
        "dombra",
        "size_display",
        "hash_display",
        "is_public",
        "locked_display",
        "uploaded_at",
    )
    list_filter = ("doc_type", "is_public")
    list_select_related = ("dombra",)
    search_fields = ("original_name", "sha256", "dombra__registration_number", "dombra__title")
    fields = (
        "dombra",
        "doc_type",
        "title",
        "file",
        "original_name",
        "size_display",
        "sha256",
        "event",
        "is_public",
        "uploaded_at",
        "uploaded_by",
    )
    readonly_fields = (
        "dombra",
        "doc_type",
        "file",
        "original_name",
        "size_display",
        "sha256",
        "event",
        "uploaded_at",
        "uploaded_by",
    )
    actions = ("verify_files",)

    def has_add_permission(self, request):
        return False  # документы добавляются со страницы домбры

    def has_delete_permission(self, request, obj=None):
        if obj is None or obj.is_locked:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description="Размер")
    def size_display(self, document):
        return filesizeformat(document.size)

    @admin.display(description="SHA-256")
    def hash_display(self, document):
        return short_hash(document.sha256)

    @admin.display(description="В истории", boolean=True)
    def locked_display(self, document):
        return document.is_locked

    @admin.action(description="Пересчитать SHA-256 файлов и сравнить с реестром")
    def verify_files(self, request, queryset):
        failed = 0
        for document in queryset:
            try:
                matches = document.compute_file_hash() == document.sha256
            except OSError:
                matches = False
            if not matches:
                failed += 1
                self.message_user(
                    request,
                    f"{document}: файл изменён или отсутствует, SHA-256 не совпадает.",
                    messages.ERROR,
                )
        if not failed:
            self.message_user(
                request, f"Проверено файлов: {queryset.count()}. Все хэши совпадают.", messages.SUCCESS
            )


# ---------------------------------------------------------------------------
# События (только просмотр)
# ---------------------------------------------------------------------------


@admin.register(RegistryEvent)
class RegistryEventAdmin(admin.ModelAdmin):
    list_display = (
        "dombra",
        "sequence",
        "event_type",
        "status_to",
        "created_at",
        "created_by_name",
        "hash_display",
    )
    list_filter = ("event_type", "created_at")
    list_select_related = ("dombra",)
    search_fields = ("dombra__registration_number", "dombra__title", "event_hash", "description")
    date_hierarchy = "created_at"
    fields = (
        "dombra",
        "sequence",
        "event_type",
        "created_at",
        "created_by",
        "created_by_name",
        "organization",
        "description",
        "status_from",
        "status_to",
        "documents_display",
        "payload_display",
        "hash_version",
        "previous_hash",
        "data_hash",
        "event_hash",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Хэш события")
    def hash_display(self, event):
        return short_hash(event.event_hash)

    @admin.display(description="Документы")
    def documents_display(self, event):
        documents = list(event.documents.all())
        if not documents:
            return "—"
        return format_html_join(
            "",
            '<div><a href="{}">{}</a> — <code>{}</code></div>',
            (
                (
                    reverse("admin:registry_document_change", args=[document.pk]),
                    str(document),
                    document.sha256,
                )
                for document in documents
            ),
        )

    @admin.display(description="Данные события (входят в хэш данных)")
    def payload_display(self, event):
        text = json.dumps(event.payload, ensure_ascii=False, indent=2, sort_keys=True)
        return format_html('<pre style="margin:0;white-space:pre-wrap;">{}</pre>', text)
