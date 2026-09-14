"""Шаг 14.09.2026: полная синхронизация Oasis как поток внутри процесса
gunicorn-воркера съела всю память хоста (1 CPU / 1 ГБ) и уронила прод.
Эти тесты фиксируют контракт замены — синхронизация ВСЕГДА в отдельном
процессе ОС, никогда прямым вызовом sync_oasis_catalog() из потока."""
import subprocess
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from tenders import scheduler
from tenders.models import CatalogSupplier, CatalogSyncRun


class RunOnceTests(SimpleTestCase):
    def test_spawns_a_subprocess_not_a_direct_call(self):
        """Регрессия к падению 14.09.2026: sync_oasis_catalog() не должен
        вызываться напрямую в потоке — только через subprocess.run(manage.py
        sync_oasis_catalog), с жёстким timeout."""
        with patch("tenders.scheduler.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            scheduler._run_once()
        run.assert_called_once()
        args, kwargs = run.call_args
        command = args[0]
        self.assertIn(scheduler.sys.executable, command)
        self.assertIn("sync_oasis_catalog", command)
        self.assertEqual(kwargs.get("timeout"), scheduler._RUN_TIMEOUT_SECONDS)

    def test_nonzero_exit_is_logged_not_raised(self):
        with patch("tenders.scheduler.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
            scheduler._run_once()  # не должно поднять исключение

    def test_timeout_is_caught_not_raised(self):
        with patch("tenders.scheduler.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
            scheduler._run_once()  # не должно поднять исключение

    def test_unexpected_error_is_caught_not_raised(self):
        with patch("tenders.scheduler.subprocess.run") as run:
            run.side_effect = OSError("boom")
            scheduler._run_once()  # не должно поднять исключение


class StartUnderTestTests(SimpleTestCase):
    def test_start_is_a_noop_under_the_test_runner(self):
        """sys.argv во время manage.py test содержит "test" — start() обязан
        молча выйти, иначе каждый тестовый прогон плодил бы демон-поток."""
        scheduler._started = False
        with patch.dict("os.environ", {"OASIS_AUTOSYNC_ENABLED": "1"}):
            with patch("tenders.scheduler.threading.Thread") as thread:
                scheduler.start()
        thread.assert_not_called()
        self.assertFalse(scheduler._started)


class OasisImportTestViewTests(TestCase):
    """Ручной запуск (без ожидания планировщика) — тот же admin-эндпоинт,
    что и у Gifts: Bearer-токен, статус, отдельный процесс ОС."""

    def setUp(self):
        self.url = reverse("oasis_import_test")
        self.token_patch = patch.dict("os.environ", {"KNOWLEDGE_SYNC_TOKEN": "secret"})
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def test_missing_token_is_forbidden(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_wrong_token_is_forbidden(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(response.status_code, 403)

    def test_valid_request_spawns_a_subprocess_and_returns_202(self):
        with patch("tenders.views.subprocess.Popen") as popen:
            response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(response.status_code, 202)
        popen.assert_called_once()
        command = popen.call_args[0][0]
        self.assertIn("sync_oasis_catalog", command)
        self.assertTrue(popen.call_args[1].get("start_new_session"))

    def test_recent_running_sync_returns_409_without_spawning_again(self):
        supplier = CatalogSupplier.objects.create(code="oasis", name="Oasis")
        CatalogSyncRun.objects.create(supplier=supplier, status="running")
        with patch("tenders.views.subprocess.Popen") as popen:
            response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(response.status_code, 409)
        popen.assert_not_called()

    def test_status_reports_the_latest_run(self):
        supplier = CatalogSupplier.objects.create(code="oasis", name="Oasis")
        CatalogSyncRun.objects.create(
            supplier=supplier, status="success", received_count=37492,
            created_count=37492, finished_at=timezone.now(),
        )
        response = self.client.get(self.url, {"status": "1"}, HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertEqual(response.json()["received"], 37492)
