"""Форма действия с домброй в панели администратора (регистрация, испытание, ремонт...)."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Document, Organization, RegistryEvent, validate_document_file
from .services import EVENT_RULES, DocumentUpload

EventType = RegistryEvent.EventType


class EventActionForm(forms.Form):
    """
    Одна форма на все типы событий: набор полей и их обязательность
    берутся из правил EVENT_RULES.
    """

    description = forms.CharField(
        label="Описание",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "cols": 80, "class": "vLargeTextField"}),
    )
    organization = forms.ModelChoiceField(
        label="Организация",
        required=False,
        queryset=Organization.objects.filter(is_active=True),
        empty_label="— выберите —",
    )
    certificate_number = forms.CharField(
        label="Номер сертификата",
        max_length=64,
        widget=forms.TextInput(attrs={"class": "vTextField"}),
    )
    certification_date = forms.DateField(
        label="Дата сертификации",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    file = forms.FileField(label="Файл", required=False)
    doc_type = forms.ChoiceField(
        label="Тип документа", required=False, choices=Document.DocType.choices
    )
    doc_title = forms.CharField(
        label="Описание документа",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "vTextField"}),
    )
    doc_is_public = forms.BooleanField(
        label="Показывать документ в публичном паспорте", required=False
    )

    def __init__(self, *args, event_type, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_type = event_type
        self.rule = EVENT_RULES[event_type]

        self.fields["description"].required = self.rule.description_required
        self.fields["organization"].required = self.rule.organization_required

        if event_type == EventType.CERTIFIED:
            self.fields["certification_date"].initial = timezone.localdate
        else:
            del self.fields["certificate_number"]
            del self.fields["certification_date"]

        if event_type == EventType.REGISTERED:
            del self.fields["organization"]

        if self.rule.allows_upload:
            self.fields["file"].required = self.rule.file_required
            self.fields["doc_type"].initial = self.rule.default_doc_type
            self.fields["doc_is_public"].initial = self.rule.default_doc_type in (
                Document.DocType.PHOTO,
                Document.DocType.CERTIFICATE,
            )
        else:
            for name in ("file", "doc_type", "doc_title", "doc_is_public"):
                del self.fields[name]

    def clean(self):
        cleaned = super().clean()
        uploaded = cleaned.get("file")
        if uploaded:
            doc_type = cleaned.get("doc_type")
            if not doc_type:
                self.add_error("doc_type", "Укажите тип документа.")
            else:
                try:
                    validate_document_file(doc_type, uploaded)
                except ValidationError as error:
                    self.add_error("file", error)
        certification_date = cleaned.get("certification_date")
        if certification_date and certification_date > timezone.localdate():
            self.add_error("certification_date", "Дата сертификации не может быть в будущем.")
        return cleaned

    def uploads(self) -> list:
        """Приложенный файл в виде, который понимает services.record_event()."""
        uploaded = self.cleaned_data.get("file")
        if not uploaded:
            return []
        return [
            DocumentUpload(
                file=uploaded,
                doc_type=self.cleaned_data["doc_type"],
                title=self.cleaned_data.get("doc_title", ""),
                is_public=self.cleaned_data.get("doc_is_public", False),
            )
        ]
