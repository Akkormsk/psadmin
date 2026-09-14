"""Эквивалентность индекса исходному поиску, включая редкие формы слов."""

from unittest.mock import patch

from django.test import SimpleTestCase

from .cascade import Cascade
from .catalog import (
    _aggregate_color_variants,
    _normalized,
    _query_stems,
    _semantic_cache,
    _semantic_candidates,
    _semantic_catalog_index,
    _stem_in_words,
    _text_search_pool,
    rebuild_catalog_embeddings,
)
from .test_cascade import TestCase, _product


class NameIndexTests(SimpleTestCase):
    def test_index_preserves_ids_hit_counts_and_order(self):
        from .name_index import rank_names

        names = ["поло", "полотенце", "кружка", "кружки", "USB-флешка", "флэш-карта",
                 "мешок", "мешка", "шопер", "шоппер", "ветровка XL", "ветровки M", "abcd", "abcde", "abcdef", "abcdfg"]
        rows = [(i, name, name.upper()) for i, name in enumerate(names, 1)]
        for phrases in ([name] for name in names):
            stems = _query_stems(phrases)
            expected = []
            for pk, name, full_name in rows:
                words = _normalized(f"{name} {full_name}").split()
                hits = sum(_stem_in_words(stem, words) for stem in stems)
                if hits:
                    expected.append((hits, pk))
            expected.sort(key=lambda row: -row[0])
            with self.subTest(phrases=phrases):
                self.assertEqual(rank_names(rows, stems), expected)

    def test_one_query_stem_counts_once_even_if_multiple_index_keys_match(self):
        from .name_index import rank_names

        self.assertEqual(rank_names([(1, "ветровка ветровки", "Ветровка")], ["ветров"]), [(1, 1)])


class IndexedSearchTests(TestCase):
    def test_normalized_family_reaches_name_filter_as_one_parent_with_children(self):
        first = _product("Флешка 16 ГБ", external_id="F16")
        second = _product("Флешка 32 ГБ", external_id="F32")
        type(first).objects.filter(pk__in=[first.pk, second.pk]).update(family_key="oasis:flash")
        first.family_key = second.family_key = "oasis:flash"

        parents = _aggregate_color_variants([first, second], "oasis")

        self.assertEqual(len(parents), 1)
        self.assertEqual({item.external_id for item in parents[0]._variant_products}, {"F16", "F32"})

    def test_index_refreshes_after_bulk_rename_and_deactivation(self):
        product = _product("Флешка", external_id="P")
        self.assertEqual(len(_text_search_pool("oasis", ["флешка"])), 1)
        type(product).objects.filter(pk=product.pk).update(name="Кружка", full_name="Кружка")
        self.assertEqual(_text_search_pool("oasis", ["флешка"]), [])
        self.assertEqual(len(_text_search_pool("oasis", ["кружка"])), 1)
        type(product).objects.filter(pk=product.pk).update(is_active=False)
        self.assertEqual(_text_search_pool("oasis", ["кружка"]), [])

    def test_supplier_and_description_do_not_leak_into_results(self):
        _product("Кружка", external_id="P", supplier_code="gifts", description="флешка")
        _product("Флешка", external_id="P", supplier_code="oasis")
        self.assertEqual(_text_search_pool("gifts", ["флешка"]), [])
        self.assertEqual(len(_text_search_pool("oasis", ["флешка"])), 1)


class SemanticCandidatesTests(TestCase):
    """Гибридное расширение пула по смыслу (лаборатория, шаг 3, настройка
    semantic=yes) — только на подложных векторах, без сети."""

    def setUp(self):
        _semantic_cache.update(at=0.0, ids=None, suppliers=None, texts=None, matrix=None)

    @staticmethod
    def _embed(product, vector):
        type(product).objects.filter(pk=product.pk).update(embedding=vector, embedding_model="test-embed")

    def test_semantically_close_candidate_without_shared_word_is_rejected(self):
        """Регрессия к находке на реальных данных: похожий вектор — не
        гарантия, что это тот же товар (созвучные бренды, см. docs — пример
        «линейка»/«Liner»). Без общего корня слова с запросом код обязан
        отбросить кандидата, даже при максимальном сходстве векторов."""
        mug = _product("Кружка керамическая", external_id="MUG")
        self._embed(mug, [1.0, 0.0])  # намеренно совпадает с вектором запроса

        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            result = _semantic_candidates("USB флешка")

        self.assertEqual(result, [])

    def test_semantically_close_candidate_with_shared_word_is_kept(self):
        drive = _product("USB накопитель, необычная модель Zorg", external_id="DRV")
        self._embed(drive, [1.0, 0.0])

        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            result = _semantic_candidates("USB флешка")

        self.assertEqual([p.external_id for p in result], ["DRV"])

    def test_supplier_filter_is_respected(self):
        oasis_item = _product("USB Zorg флешка", external_id="O1", supplier_code="oasis")
        self._embed(oasis_item, [1.0, 0.0])
        gifts_item = _product("USB Zorg флешка", external_id="G1", supplier_code="gifts")
        self._embed(gifts_item, [1.0, 0.0])

        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            result = _semantic_candidates("USB флешка", supplier_codes={"oasis"})

        self.assertEqual([p.external_id for p in result], ["O1"])

    def test_products_without_an_index_are_ignored_without_spending_on_a_call(self):
        _product("USB Zorg флешка", external_id="NOIDX")  # embedding остаётся [] по умолчанию
        with patch("tenders.services._embedding_vectors") as embed, \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            result = _semantic_candidates("USB флешка")
        self.assertEqual(result, [])
        embed.assert_not_called()

    def test_empty_query_returns_nothing_without_calling_the_gateway(self):
        with patch("tenders.services._embedding_vectors") as embed:
            result = _semantic_candidates("   ")
        self.assertEqual(result, [])
        embed.assert_not_called()

    def test_index_is_cached_across_calls(self):
        drive = _product("USB Zorg флешка", external_id="DRV")
        self._embed(drive, [1.0, 0.0])
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            _semantic_candidates("USB флешка")
        with patch("tenders.catalog.CatalogProduct.objects") as objects:
            ids, suppliers, texts, matrix = _semantic_catalog_index()
        objects.filter.assert_not_called()
        self.assertIsNotNone(matrix)


class RebuildCatalogEmbeddingsTests(TestCase):
    def test_skips_products_whose_hash_and_model_already_match(self):
        _product("USB флешка", external_id="P")
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"), \
             patch("tenders.gateway_budget.preflight"):
            first = rebuild_catalog_embeddings()
        self.assertEqual((first["embedded"], first["skipped"]), (1, 0))

        with patch("tenders.services._embedding_vectors") as embed, \
             patch("tenders.services._embedding_model", return_value="test-embed"), \
             patch("tenders.gateway_budget.preflight"):
            second = rebuild_catalog_embeddings()
        embed.assert_not_called()
        self.assertEqual((second["embedded"], second["skipped"]), (0, 1))

    def test_renamed_product_gets_reembedded(self):
        product = _product("USB флешка", external_id="P")
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"), \
             patch("tenders.gateway_budget.preflight"):
            rebuild_catalog_embeddings()
        type(product).objects.filter(pk=product.pk).update(name="Кружка", full_name="Кружка")

        with patch("tenders.services._embedding_vectors", return_value=[[0.0, 1.0]]) as embed, \
             patch("tenders.services._embedding_model", return_value="test-embed"), \
             patch("tenders.gateway_budget.preflight"):
            result = rebuild_catalog_embeddings()
        embed.assert_called_once()
        self.assertEqual(result["embedded"], 1)

    def test_respects_limit_and_reports_remaining_honestly(self):
        _product("USB флешка 1", external_id="P1")
        _product("USB флешка 2", external_id="P2")
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"), \
             patch("tenders.gateway_budget.preflight"):
            result = rebuild_catalog_embeddings(limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["embedded"], 1)
        self.assertEqual(result["remaining"], 1)


class Step3SemanticSettingTests(TestCase):
    """Настройка step_3.semantic — по умолчанию выключена (обычный подбор
    её не трогает), включается явно только для этого прогона (лаборатория)."""

    def setUp(self):
        _semantic_cache.update(at=0.0, ids=None, suppliers=None, texts=None, matrix=None)

    def test_semantic_off_by_default_no_gateway_call(self):
        _product("Флешка", external_id="LEX")
        cascade = Cascade({"name": "флешка"}, step_settings={})
        with patch("tenders.services._embedding_vectors") as embed:
            pool = cascade.step_3_search_by_name(["флешка"])
        embed.assert_not_called()
        self.assertEqual({p.external_id for p in pool}, {"LEX"})
        self.assertNotIn("semantic_added", cascade.diagnostics)

    def test_semantic_on_adds_new_candidates_and_reports_the_count(self):
        """SEM не разделяет ни одного слова с лексической фразой поиска
        («флеш-накопитель») — лексика его не найдёт. Но разделяет корень с
        именем позиции («USB stick») — по нему его находит смысловой слой."""
        _product("Флеш-накопитель классический", external_id="LEX")
        extra = _product("USB Zorg stick", external_id="SEM")
        type(extra).objects.filter(pk=extra.pk).update(embedding=[1.0, 0.0], embedding_model="test-embed")

        cascade = Cascade({"name": "USB stick"}, step_settings={"3": {"semantic": "yes"}})
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            pool = cascade.step_3_search_by_name(["флеш-накопитель"])

        self.assertEqual({p.external_id for p in pool}, {"LEX", "SEM"})
        self.assertEqual(cascade.diagnostics["semantic_added"], 1)

    def test_semantic_does_not_duplicate_a_card_already_found_by_lexical_search(self):
        both = _product("USB Zorg накопитель", external_id="BOTH")
        type(both).objects.filter(pk=both.pk).update(embedding=[1.0, 0.0], embedding_model="test-embed")

        cascade = Cascade({"name": "накопитель"}, step_settings={"3": {"semantic": "yes"}})
        with patch("tenders.services._embedding_vectors", return_value=[[1.0, 0.0]]), \
             patch("tenders.services._embedding_model", return_value="test-embed"):
            pool = cascade.step_3_search_by_name(["накопитель"])

        self.assertEqual([p.external_id for p in pool], ["BOTH"])
        self.assertEqual(cascade.diagnostics["semantic_added"], 0)
