"""
Проверка целостности реестра из терминала.

    python manage.py verify_registry                      # все домбры, с пересчётом файлов
    python manage.py verify_registry --no-files           # только цепочки событий
    python manage.py verify_registry DOM-KZ-2026-000001   # одна или несколько домбр

Код возврата 1, если найдено хотя бы одно нарушение (удобно для автоматических проверок).
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from registry.models import Dombra
from registry.services import verify_dombra


class Command(BaseCommand):
    help = "Пересчитывает цепочки хэшей событий и SHA-256 файлов, сообщает о расхождениях."

    def add_arguments(self, parser):
        parser.add_argument(
            "numbers",
            nargs="*",
            help="Регистрационные номера. Если не указаны, проверяется весь реестр.",
        )
        parser.add_argument(
            "--no-files",
            action="store_true",
            help="Не пересчитывать хэши файлов (быстрее).",
        )

    def handle(self, *args, numbers, no_files, **options):
        queryset = Dombra.objects.order_by("registration_number", "created_at")
        if numbers:
            queryset = queryset.filter(registration_number__in=numbers)
            found = set(queryset.values_list("registration_number", flat=True))
            missing = [number for number in numbers if number not in found]
            if missing:
                raise CommandError(f"В реестре нет домбр с номерами: {', '.join(missing)}")

        total = failed = 0
        for dombra in queryset.iterator():
            result = verify_dombra(dombra, check_files=not no_files)
            total += 1
            if result.ok:
                self.stdout.write(self.style.SUCCESS(f"OK    {dombra} — {result.summary()}"))
                continue
            failed += 1
            self.stdout.write(self.style.ERROR(f"FAIL  {dombra} — {result.summary()}"))
            for problem in result.problems:
                self.stdout.write(f"        - {problem}")

        self.stdout.write("")
        if failed:
            self.stdout.write(self.style.ERROR(f"Проверено домбр: {total}. С нарушениями: {failed}."))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(f"Проверено домбр: {total}. Нарушений нет."))
