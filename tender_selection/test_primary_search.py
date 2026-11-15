import os
import re
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase, TransactionTestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from . import services, gosplan
from .models import FilterSettings, DiscoveryCursor
from .primary_search import PrimaryFilterForm, build_primary_params, word_pattern


class PrimaryPatternTests(SimpleTestCase):
    def test_literal_or_case_yo_and_compounds(self):
        pattern = word_pattern('Сувенир, Ёлоч, футболк+принт, (печать)')
        for title in ['СУВЕНИРЫ', 'елочка', 'ЁЛОЧНЫЕ', 'Футболки', '(печать)']:
            self.assertRegex(title, pattern)
        self.assertIsNone(re.search(pattern, 'щебень'))
        self.assertIsNone(word_pattern(''))
        with self.assertRaises(ValueError):
            word_pattern('аб')

    def test_invalid_ranges_and_unknown_fields(self):
        from .primary_search import validate_primary
        for value in [{'published_after': '2026-11-20', 'published_before': '2026-11-01'},
                      {'contract_guarantee_part_ge': 101}, {'stage': ['9']}, {'url': 'bad'}]:
            with self.assertRaises(ValueError):
                validate_primary(value)


class PrimarySearchTests(TestCase):
    def setUp(self):
        self.settings = FilterSettings.load()
        self.settings.laws = ['fz44']
        self.settings.include_words = 'сувенир, полиграф'
        self.settings.save()

    def test_default_is_editable_and_clear_stays_clear(self):
        self.assertEqual(self.settings.primary_filters['stage'], ['1'])
        services.save_selection_settings({'primary_filters': {'stage': []}})
        settings = FilterSettings.load()
        self.assertNotIn('stage', build_primary_params(settings, 'fz44', timezone.now()))
        services.save_selection_settings({'primary_filters': {'max_price_le': '2000000'}})
        self.assertEqual(FilterSettings.load().primary_filters['stage'], [])

    def test_primary_conditions_and_unbounded_publication(self):
        services.save_selection_settings({'primary_filters': {'stage': ['1', '2'], 'max_price_le': '2000000'},
                                         'regions': ['77'], 'okpd2_codes': []})
        with patch.object(gosplan, 'iter_purchases', return_value=iter([])) as network:
            result = services.run_pull()
        self.assertTrue(result.ok)
        self.assertEqual(network.call_count, 1)
        params = network.call_args.kwargs['params']
        self.assertEqual(params['stage'], ['1', '2'])
        self.assertEqual(params['region'], [77])
        self.assertEqual(params['max_price_le'], '2000000')
        self.assertIn('|', params['object_info'])
        self.assertNotIn('classifier', params)
        self.assertTrue(params['published_after'].startswith('2024-01-01'))
        self.assertEqual(result.params['jobs_total'], 1)

    def test_laws_and_classifiers_are_separate_jobs_with_same_words(self):
        services.save_selection_settings({'laws': ['fz44', 'fz223'], 'okpd2_codes': ['17', '18']})
        with patch.object(gosplan, 'iter_purchases', side_effect=lambda **kw: iter([])) as network:
            result = services.run_pull()
        self.assertTrue(result.ok)
        self.assertEqual(network.call_count, 4)
        self.assertEqual({c.kwargs['law'] for c in network.call_args_list}, {'fz44', 'fz223'})
        self.assertTrue(all(len(c.kwargs['params']['classifier']) == 1 for c in network.call_args_list))
        self.assertEqual(len({c.kwargs['params']['object_info'] for c in network.call_args_list}), 1)

    def test_law_specific_fields_and_deadlines(self):
        services.save_selection_settings({'primary_filters': {'deadline_after': '2026-11-01T12:00:00Z',
            'drug': 'false', 'customer': '1234567890', 'placer': '0987654321', 'ikz': '123'}})
        settings = FilterSettings.load()
        first = build_primary_params(settings, 'fz44', timezone.now())
        second = build_primary_params(settings, 'fz223', timezone.now())
        self.assertIs(first['drug'], False)
        self.assertIn('collecting_finished_after', first)
        self.assertIn('submission_close_after', second)
        self.assertNotIn('drug', second)
        self.assertNotIn('ikz', second)
        self.assertNotIn('placer', first)

    def test_invalid_settings_roll_back_all_changes(self):
        for payload in [{'min_price': 3000000, 'primary_filters': {'max_price_le': 2000000}},
                        {'include_words': 'changed', 'primary_filters': {'stage': ['99']}},
                        {'okpd2_codes': ['bad']}, {'regions': ['999']}]:
            with self.assertRaises(ValueError):
                services.save_selection_settings(payload)
        self.assertEqual(FilterSettings.load().include_words, self.settings.include_words)
        self.assertEqual(FilterSettings.load().min_price, self.settings.min_price)

    def test_date_filter_change_creates_separate_cursor(self):
        with patch.object(gosplan, 'iter_purchases', side_effect=lambda **kw: iter([])):
            services.run_pull()
            services.save_selection_settings({'primary_filters': {'published_after': '2026-01-01T00:00:00Z'}})
            services.run_pull()
        self.assertEqual(DiscoveryCursor.objects.count(), 2)

    def test_mirror_does_not_activate_collector(self):
        with patch.dict(os.environ, {'TENDER_DISCOVERY_ROLE': 'mirror'}), \
             patch('tender_selection.discovery_feed.import_feed') as mirror, \
             patch.object(gosplan, 'iter_purchases') as network:
            services.run_pull()
        mirror.assert_called_once()
        network.assert_not_called()

    def test_settings_ui_round_trip_clear_and_permissions(self):
        url = reverse('tender_selection:settings')
        self.assertNotEqual(self.client.get(url).status_code, 200)
        admin = get_user_model().objects.create_superuser('primary-admin', 'p@example.test', 'pass')
        self.client.force_login(admin)
        response = self.client.get(url)
        self.assertContains(response, 'Основной фильтр')
        self.assertContains(response, 'Вторичный фильтр')
        payload = {'settings_version': '2', 'source': 'eis', 'law': 'fz44', 'min_price': '300000',
                   'include_words': 'сувенир', 'okpd2_codes': '', 'max_price_le': '2000000'}
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(FilterSettings.load().primary_filters['stage'], [])
        payload['max_price_le'] = ''
        self.client.post(url, payload)
        self.assertNotIn('max_price_le', FilterSettings.load().primary_filters)
        self.assertContains(self.client.get(url), 'value="2"')

    def test_assistant_uses_same_validation_and_requires_confirmation(self):
        from tenders.assistant_tools import execute_tool, AssistantToolError
        admin = get_user_model().objects.create_superuser('assistant-admin', 'a@example.test', 'pass')
        with self.assertRaises(AssistantToolError):
            execute_tool('tender.discovery_settings', admin, {'primary_filters': {'stage': []}})
        execute_tool('tender.discovery_settings', admin, {'primary_filters': {'stage': []}}, confirmed=True)
        self.assertEqual(FilterSettings.load().primary_filters['stage'], [])
        with self.assertRaises(AssistantToolError):
            execute_tool('tender.discovery_settings', admin, {'primary_filters': {'stage': ['9']}}, confirmed=True)


class PrimaryTransportTests(SimpleTestCase):
    def test_long_or_list_is_split_without_losing_terms(self):
        from .primary_search import split_primary_requests
        words = ','.join('полиграфическаяпродукция' + str(i) for i in range(111))
        base = {'stage': ['1'], 'max_price_ge': '300000', 'object_info': word_pattern(words)}
        groups = split_primary_requests(base, words, 'fz44')
        self.assertGreater(len(groups), 1)
        self.assertEqual(sum(g['object_info'].count('|') + 1 for g in groups), 111)
        for i in range(111):
            self.assertTrue(any(re.search(g['object_info'], 'полиграфическаяпродукция'+str(i)) for g in groups))
        self.assertTrue(all(g['stage'] == ['1'] and g['max_price_ge'] == '300000' for g in groups))

    def test_provider_date_partition_covers_entire_range(self):
        from datetime import datetime, timedelta
        params = {'published_after': '2024-01-01T00:00:00+00:00', 'published_before': '2026-11-15T00:00:00+00:00'}
        with patch.object(gosplan, 'fetch_page', return_value=[]) as request:
            self.assertEqual(list(gosplan.iter_purchases(params=params)), [])
        queries = [c.args[0] for c in request.call_args_list]
        self.assertEqual(queries[0]['published_after'], params['published_after'])
        self.assertEqual(queries[-1]['published_before'], params['published_before'])
        for query in queries:
            self.assertLessEqual(datetime.fromisoformat(query['published_before']) - datetime.fromisoformat(query['published_after']), timedelta(days=90))
        for a, b in zip(queries, queries[1:]):
            self.assertEqual(a['published_before'], b['published_after'])
        with patch.object(gosplan, 'fetch_page', return_value=[]):
            with self.assertRaises(gosplan.GosplanError):
                list(gosplan.iter_purchases(params=params, max_requests=1))


class PrimarySettingsMigrationTests(TransactionTestCase):
    def tearDown(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_selection_survives_forward_and_reverse(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        before = [('tender_selection', '0048_tender_history_identity')]
        after = [('tender_selection', '0049_primary_search_filters')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        model = executor.loader.project_state(before).apps.get_model('tender_selection', 'FilterSettings')
        model.objects.update_or_create(pk=1, defaults=dict(include_words='сувенир, полиграф', okpd2_codes=['17'],
                             regions=['77'], laws=['fz223'], min_price='400000', window_days=14))
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        model = executor.loader.project_state(after).apps.get_model('tender_selection', 'FilterSettings')
        value = model.objects.get(pk=1)
        self.assertEqual(value.primary_filters, {'stage': ['1']})
        self.assertEqual(value.include_words, 'сувенир, полиграф')
        self.assertEqual(value.okpd2_codes, ['17'])
        self.assertEqual(value.laws, ['fz223'])
        self.assertEqual(value.window_days, 14)
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        model = executor.loader.project_state(before).apps.get_model('tender_selection', 'FilterSettings')
        self.assertEqual(model.objects.get(pk=1).include_words, 'сувенир, полиграф')


class PrimaryRoundTripTests(SimpleTestCase):
    def test_saved_date_renders_in_editable_local_time(self):
        with timezone.override('Europe/Moscow'):
            initial = {'published_after': '2026-11-01T12:00:45+00:00'}
            form = PrimaryFilterForm(initial=initial)
            self.assertIn('value="2026-11-01T15:00:45"', str(form['published_after']))
            self.assertEqual(initial['published_after'], '2026-11-01T12:00:45+00:00')

    def test_request_groups_leave_room_for_incremental_cursor_and_classifier(self):
        from .primary_search import split_primary_requests
        from urllib.parse import urlencode
        words = ','.join('сувенирнаяпродукция' + str(i) for i in range(20))
        base = {'stage': ['1'], 'object_info': word_pattern(words)}
        with patch.dict(os.environ, {'GOSPLAN_MAX_REQUEST_LINE_BYTES': '700'}):
            groups = split_primary_requests(base, words, 'fz44')
        for group in groups:
            actual = {**group, 'updated_after': '2026-11-15T00:00:00.123456+00:00',
                      'updated_before': '2026-11-15T00:01:00.123456+00:00', 'classifier': ['1'*50]}
            line = 'GET /fz44/purchases?' + urlencode(actual, doseq=True) + '&limit=100&skip=1000 HTTP/1.1\r\n'
            self.assertLessEqual(len(line.encode()), 700)
