from pathlib import Path

from django.test import SimpleTestCase

from .management.commands.benchmark_document_composition import _bounded_documents, _load_fixture, _summarize


class DocumentCompositionBenchmarkTests(SimpleTestCase):
    def test_reviewed_fixture_preserves_every_document_without_filename_filtering(self):
        fixture = Path(__file__).parent / "fixtures" / "document_composition_benchmark.json"
        title, items, documents = _load_fixture(str(fixture))
        payload = _bounded_documents(documents)

        self.assertTrue(title)
        self.assertEqual([item["id"] for item in items], [1, 2])
        self.assertEqual([item["url"] for item in payload], [item["url"] for item in documents])
        self.assertEqual([item["name"] for item in payload], [item["name"] for item in documents])

    def test_summary_accepts_only_evidenced_rows_attached_to_known_input(self):
        result = _summarize(
            {"items": [
                {"parent_source_item_id": 1, "name": "Cup", "source_url": "https://a", "relationship": "component", "quantity_per_parent": 1, "evidence": "explicit"},
                {"parent_source_item_id": 99, "name": "Invented", "source_url": "https://a", "relationship": "component"},
            ]},
            {1}, {"https://a"},
        )

        self.assertEqual(result["valid_rows"], 1)
        self.assertEqual(result["invalid_rows"], 1)
        self.assertEqual(result["items"][0]["quantity_per_parent"], 1)
