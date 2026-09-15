import importlib
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from .models import CascadeConfigVersion


class StableConfigMigrationTests(TestCase):
    def test_clean_active_version_preserves_history_and_other_steps(self):
        user = get_user_model().objects.create(username="config-owner")
        settings = {"steps": {"3": {"sources": "oasis", "semantic": "yes"}, "6": {"ceiling": 42}}, "top": 10}
        old = CascadeConfigVersion.objects.create(name="Active", created_by=user, settings=settings, is_active=True)
        migration = importlib.import_module("tenders.migrations.0030_clean_active_catalog_search")
        migration.clean_active_search(apps, SimpleNamespace(connection=connection))
        old.refresh_from_db()
        active = CascadeConfigVersion.objects.get(is_active=True)
        self.assertFalse(old.is_active)
        self.assertEqual(old.settings, settings)
        self.assertNotEqual(old.pk, active.pk)
        self.assertEqual(active.settings, {"steps": {"3": {"sources": "oasis"}, "6": {"ceiling": 42}}, "top": 10})
        migration.clean_active_search(apps, SimpleNamespace(connection=connection))
        self.assertEqual(CascadeConfigVersion.objects.count(), 2)
