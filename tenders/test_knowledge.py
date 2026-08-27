import json
import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from .models import ProductionTrainingExample, ProductionType, TenderKnowledgeSource
from .services import _training_examples_for_line


class KnowledgeBundleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.production_type = ProductionType.objects.create(code="textile_test", name="Текстиль")
        self.example = ProductionTrainingExample.objects.create(
            production_type=self.production_type,
            position_name="Жилет сигнальный",
            requirements={"requirements": [{"label": "Цвет", "value": "оранжевый"}]},
            features=["светоотражающие полосы"],
            routes=[{"name": "Закупка готового изделия"}],
            note="Подтверждено администратором",
            created_by=self.user,
        )
        self.source = TenderKnowledgeSource.objects.create(
            title="Жилеты поставщика",
            supplier_name="Поставщик",
            source_type="catalog",
            url="https://supplier.example/vests",
            content_summary="Сигнальные жилеты",
            structured_data={"scope": "position"},
            created_by=self.user,
        )

    def test_export_and_import_are_portable_and_idempotent(self):
        path = Path(os.getcwd()) / ".test-assistant-knowledge.json"
        try:
            call_command("export_assistant_knowledge", path)
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["training_examples"][0]["knowledge_id"], str(self.example.knowledge_id))
            self.assertNotIn("embedding", payload["training_examples"][0])

            ProductionTrainingExample.objects.all().delete()
            TenderKnowledgeSource.objects.all().delete()
            call_command("import_assistant_knowledge", path)
            call_command("import_assistant_knowledge", path, user="admin")
        finally:
            path.unlink(missing_ok=True)

        imported = ProductionTrainingExample.objects.get()
        self.assertEqual(imported.knowledge_id, self.example.knowledge_id)
        self.assertEqual(imported.routes[0]["name"], "Закупка готового изделия")
        self.assertEqual(TenderKnowledgeSource.objects.get().knowledge_id, self.source.knowledge_id)


class TrainingEmbeddingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin", password="password")
        self.production_type = ProductionType.objects.create(code="textile_test", name="Текстиль")

    @patch.dict(os.environ, {"TIMEWEB_EMBEDDINGS_ENABLED": "1", "TIMEWEB_EMBEDDING_MODEL": "test-embedding"})
    @patch("tenders.services._embedding_vector", return_value=[1.0, 0.0])
    def test_semantic_search_prefers_meaning_over_shared_words(self, embedding_vector):
        semantic = ProductionTrainingExample.objects.create(
            production_type=self.production_type,
            position_name="Сигнальная безрукавка",
            features=["защитная одежда"],
            created_by=self.user,
            embedding=[1.0, 0.0],
            embedding_model="test-embedding",
        )
        ProductionTrainingExample.objects.create(
            production_type=self.production_type,
            position_name="Жилет для документов",
            features=["офисный аксессуар"],
            created_by=self.user,
            embedding=[0.0, 1.0],
            embedding_model="test-embedding",
        )

        result = _training_examples_for_line({"name": "Жилет светоотражающий"})

        self.assertEqual(result[0], semantic)
        embedding_vector.assert_called_once()

    @patch.dict(os.environ, {"TIMEWEB_EMBEDDINGS_ENABLED": "1", "TIMEWEB_EMBEDDING_MODEL": "test-embedding"})
    @patch("tenders.services._embedding_vector", return_value=[0.25, 0.75])
    def test_refresh_command_stores_embedding(self, embedding_vector):
        example = ProductionTrainingExample.objects.create(
            production_type=self.production_type,
            position_name="Жилет утеплённый",
            features=["одежда"],
            created_by=self.user,
        )

        call_command("refresh_training_embeddings")

        example.refresh_from_db()
        self.assertEqual(example.embedding, [0.25, 0.75])
        self.assertEqual(example.embedding_model, "test-embedding")
        embedding_vector.assert_called_once()
