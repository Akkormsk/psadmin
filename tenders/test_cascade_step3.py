"""Эквивалентность индекса исходному поиску, включая редкие формы слов."""

from django.test import SimpleTestCase

from .catalog import _normalized, _query_stems, _stem_in_words, _text_search_pool
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
