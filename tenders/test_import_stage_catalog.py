import io

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from openpyxl import Workbook

from .models import ProcessDefinition, Proposal


def _workbook_path(tmp_path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Название", "Даёт заготовку/материал", "Выполняет производство", "Завершает маршрут",
        "Что производим", "Когда использовать", "Когда не использовать", "Что обязательно знать из ТЗ",
    ])
    for row in rows:
        ws.append(row)
    path = tmp_path / "stages.xlsx"
    wb.save(path)
    return str(path)


class ImportStageCatalogTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username="admin", password="password")

    def test_imports_a_stage_with_structured_required_parameters(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = _workbook_path(Path(tmp), [(
                "УФ-печать", "нет", "да", "иногда",
                "Нанесение на готовые изделия",
                "Есть готовая заготовка",
                "Текстиль",
                "Тираж; материал изделия; размер нанесения; наличие белил",
            )])
            call_command("import_stage_catalog", path)

        stage = ProcessDefinition.objects.get(name="УФ-печать")
        self.assertFalse(stage.supplies_input)
        self.assertTrue(stage.performs_production)
        self.assertEqual(stage.terminal_mode, "sometimes")
        self.assertEqual(stage.scope_tags, ["Нанесение на готовые изделия"])
        self.assertEqual(
            stage.parameters["required"],
            ["Тираж", "материал изделия", "размер нанесения", "наличие белил"],
        )
        self.assertEqual(stage.parameters["optional"], [])
        proposal = Proposal.objects.get(payload__name="УФ-печать")
        self.assertEqual(proposal.status, Proposal.STATUS_ACCEPTED)
        self.assertEqual(proposal.type, Proposal.TYPE_CREATE_STAGE)

    def test_reimport_updates_the_existing_stage_instead_of_duplicating(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = _workbook_path(Path(tmp), [(
                "Тампопечать", "нет", "да", "иногда", "Ручки, брелоки",
                "Небольшая зона нанесения", "Полноцветное изображение", "Тираж; материал",
            )])
            call_command("import_stage_catalog", path)
            call_command("import_stage_catalog", path)

        self.assertEqual(ProcessDefinition.objects.filter(name="Тампопечать").count(), 1)

    def test_row_without_a_name_is_skipped_not_crashed(self):
        import tempfile
        from pathlib import Path

        before = ProcessDefinition.objects.count()  # миграция 0004 сеет базовый набор этапов
        with tempfile.TemporaryDirectory() as tmp:
            path = _workbook_path(Path(tmp), [(None, "да", "нет", "иногда", "", "", "", "")])
            call_command("import_stage_catalog", path)

        self.assertEqual(ProcessDefinition.objects.count(), before)
