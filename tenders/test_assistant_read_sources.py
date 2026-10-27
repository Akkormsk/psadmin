"""READ source isolation. Scripted Gateway checks backend, not model quality."""
import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from cash.models import CashTransaction
from .assistant_agent import AgentProtocolError, _normalise_plan
from .assistant_conversations import create_conversation, respond
from .assistant_tools import AssistantReferenceError
from .test_assistant_conversations import function_call, planned_call


class AssistantReadSourceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('read-sources', 'read@example.test', 'password')
        self.conversation = create_conversation(self.user)
        for i in range(25):
            match = i < 5 or i % 4 == 0
            CashTransaction.objects.create(operation_date=date(2026, 10, 27), account='cash' if match else 'card',
                direction='expense' if match else 'income', amount=str(i + 1), reason=f'SYNTHETIC-{i}', created_by=self.user)
        self.base = {'fields': ['id', 'account', 'direction', 'amount'], 'order_by': ['-id'], 'limit': 20}
        self.filters = [{'field': 'account', 'op': 'eq', 'value': 'cash'}, {'field': 'direction', 'op': 'eq', 'value': 'expense'}]

    def plan(self, source=None, step='read', revision=None):
        return planned_call('read:cash.transactions', steps=[{'id': step, 'capability_id': 'read:cash.transactions',
            'objective': step, 'expected_result': 'Строки', 'reference_sources': [] if source is None else [{'parameter': 'records', 'read_id': source}]}], revision_reason=revision)

    def run_calls(self, calls, text):
        with patch('tenders.assistant_agent._gateway', side_effect=calls), patch('tenders.assistant_agent.preflight'):
            return respond(self.conversation, self.user, text)[-1]

    def first(self, filters=None):
        result = self.run_calls([self.plan(), function_call('read_cash_transactions', {'plan': {**self.base, 'filters': filters or []}}),
            function_call('assistant_finish', {'answer': 'Показаны операции.'})], 'Покажи последние 20 операций')
        return next(s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:'))

    def test_missing_read_source_declaration_is_rejected(self):
        with self.assertRaises(AssistantReferenceError):
            _normalise_plan({'goal': 'Чтение', 'steps': [{'id': 'r', 'capability_id': 'read:cash.transactions', 'objective': 'r'}]}, self.user)

    def test_saved_twenty_bound_filter_order_limit_and_aggregate(self):
        first = self.first()
        expected = [r['id'] for r in first['records'] if r['account'] == 'cash' and r['direction'] == 'expense']
        result = self.run_calls([self.plan(first['source_id']), function_call('read_cash_transactions', {'plan': {
            **self.base, 'filters': self.filters, 'order_by': ['-amount'], 'limit': 2,
            'aggregates': [{'op': 'count', 'field': 'id'}]}}), function_call('assistant_finish', {'answer': 'Готово.'})], 'Из них два самых больших наличных расхода и количество')
        read = next(s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:'))
        self.assertEqual(read['total_count'], len(expected))
        self.assertTrue(all(r['id'] in expected for r in read['records']))
        self.assertEqual(len(read['records']), 2)
        self.assertEqual(list(read['aggregates'].values()), [len(expected)])

    def test_empty_source_never_expands_to_global_matches(self):
        first = self.first([{'field': 'id', 'op': 'eq', 'value': 999999}])
        result = self.run_calls([self.plan(first['source_id']), function_call('read_cash_transactions', {'plan': {**self.base, 'filters': self.filters}}),
            function_call('assistant_finish', {'answer': 'Совпадений нет.'})], 'Из них наличные расходы')
        self.assertEqual(next(s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:'))['records'], [])

    def test_independent_read_is_not_bound_to_previous_selection(self):
        self.first()
        result = self.run_calls([self.plan(), function_call('read_cash_transactions', {'plan': {**self.base, 'filters': self.filters}}),
            function_call('assistant_finish', {'answer': 'Готово.'})], 'Теперь по всей кассе наличные расходы')
        self.assertEqual(next(s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:'))['total_count'], 10)

    def test_future_or_wrong_entity_sources_are_rejected(self):
        for source, state in [('future', {}), ('wrong', {'memory': {'recent_selections': [{'source_id': 'wrong', 'entity': 'tender_selection.Tender', 'record_ids': [1]}]}})]:
            value = json.loads(self.plan(source)['tool_calls'][0]['function']['arguments'])['plan']
            with self.assertRaises(AssistantReferenceError):
                _normalise_plan(value, self.user, state=state)

    def test_same_cycle_source_is_bound_before_read_and_cannot_be_swapped_by_arguments(self):
        steps = [dict(id='first', capability_id='read:cash.transactions', objective='Первый', expected_result='20 строк', reference_sources=[]),
                 dict(id='second', capability_id='read:cash.transactions', objective='Из них', expected_result='Расходы', reference_sources=[{'parameter': 'records', 'read_id': 'first'}])]
        result = self.run_calls([planned_call('read:cash.transactions', steps=steps),
            function_call('read_cash_transactions', {'plan': self.base}),
            function_call('read_cash_transactions', {'plan': {**self.base, 'filters': self.filters}}),
            function_call('assistant_finish', {'answer': 'Готово.'})], 'Последние 20, из них наличные расходы')
        reads = [s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:')]
        self.assertEqual(reads[1]['total_count'], 5)
        self.assertTrue({r['id'] for r in reads[1]['records']} <= {r['id'] for r in reads[0]['records']})

    def test_complete_cache_and_orm_fallback_obey_same_saved_source(self):
        from .assistant_agent import _scoped_read_plan
        from .assistant_read import execute, reuse_result

        first = self.first([{'field': 'id', 'op': 'lte', 'value': 4}])
        state = {'memory': self.conversation.context['agent_memory'], 'active_plan_step_id': 'read'}
        value = json.loads(self.plan(first['source_id'])['tool_calls'][0]['function']['arguments'])['plan']
        state['plan'] = _normalise_plan(value, self.user, state=state)
        query = _scoped_read_plan(state, {**self.base, 'entity': 'cash.CashTransaction',
            'filters': [*self.filters, {'field': 'id', 'op': 'lte', 'value': 4}]})
        cached = reuse_result(self.user, query, first)
        self.assertIsNotNone(cached)
        self.assertEqual(cached['source_scope'], 'cached complete result')
        fresh = execute(self.user, {**query, 'order_by': ['amount']})
        self.assertEqual({r['id'] for r in fresh['records']}, {r['id'] for r in cached['records']})
        self.assertEqual(fresh['total_count'], 4)

    def test_explicit_older_selection_wins_over_latest_selection(self):
        first = self.first()
        self.first([{'field': 'id', 'op': 'lte', 'value': 4}])
        result = self.run_calls([self.plan(first['source_id']), function_call('read_cash_transactions', {'plan': {
            **self.base, 'filters': self.filters}}), function_call('assistant_finish', {'answer': 'Готово.'})], 'Вернись к первым двадцати, из них наличные расходы')
        read = next(s['result'] for s in result['data']['steps'] if s['tool_id'].startswith('read:'))
        self.assertEqual(read['total_count'], 5)
        self.assertTrue({r['id'] for r in read['records']} <= {r['id'] for r in first['records']})

    def test_source_change_requires_replanning_and_new_output_identity(self):
        first = self.first()
        state = {'memory': self.conversation.context['agent_memory']}
        value = json.loads(self.plan(first['source_id'])['tool_calls'][0]['function']['arguments'])['plan']
        old = _normalise_plan(value, self.user, state=state)
        value['steps'][0]['reference_sources'] = []
        with self.assertRaises(AgentProtocolError) as error:
            _normalise_plan(value, self.user, previous=old, state=state)
        self.assertIn('Пересмотр плана', str(error.exception))
        value['revision_reason'] = 'Пользователь явно расширил поиск на всю базу'
        new = _normalise_plan(value, self.user, previous=old, state=state)
        self.assertNotEqual(old['steps'][0]['source_id'], new['steps'][0]['source_id'])

    def test_incomplete_or_grouped_source_cannot_turn_into_unbounded_read(self):
        from .assistant_agent import _scoped_read_plan

        for source in [
            {'source_id': 'bad', 'entity': 'cash.CashTransaction', 'record_ids': [1], 'returned_count': 20},
            {'source_id': 'bad', 'entity': 'cash.CashTransaction', 'record_ids': [], 'returned_count': 0, 'query': {'group_by': ['account']}},
        ]:
            state = {'memory': {'recent_selections': [source]}, 'active_plan_step_id': 'read'}
            value = json.loads(self.plan('bad')['tool_calls'][0]['function']['arguments'])['plan']
            state['plan'] = _normalise_plan(value, self.user, state=state)
            with self.assertRaises(AssistantReferenceError):
                _scoped_read_plan(state, {**self.base, 'entity': 'cash.CashTransaction'})

    def test_unrelated_action_keeps_nullable_reference_declaration(self):
        value = {'goal': 'Расход', 'steps': [{'id': 'act', 'capability_id': 'cash.transaction_create',
            'objective': 'Создать расход', 'reference_sources': None}]}
        plan = _normalise_plan(value, self.user)
        self.assertEqual(plan['steps'][0]['capability_id'], 'cash.transaction_create')
