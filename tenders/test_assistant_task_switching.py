"""Characterise task switching without pretending scripted calls prove model reliability."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from cash.models import CashTransaction
from .assistant_agent import continue_after_cancellation
from .assistant_conversations import create_conversation, respond
from .test_assistant_conversations import function_call, planned_call


class AssistantTaskSwitchingDiagnostics(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('task-switch', 'switch@example.test', 'password')
        gateway = patch('tenders.assistant_agent._gateway')
        self.gateway = gateway.start()
        self.addCleanup(gateway.stop)
        budget = patch('tenders.assistant_agent.preflight')
        budget.start()
        self.addCleanup(budget.stop)

    def open_task(self, mode):
        conversation = create_conversation(self.user)
        if mode == 'cancelled_card':
            self.gateway.side_effect = [
                planned_call('cash.transaction_create'),
                function_call('cash_transaction_create', {'operation_date': '2026-10-25',
                    'account': 'cash', 'direction': 'expense', 'amount': 260, 'reason': 'Покупка коробок'}),
                function_call('assistant_ask', {'question': 'Что изменить?'}),
            ]
            card = respond(conversation, self.user, 'Подготовь расход наличными 260 рублей за покупку коробок на 25.10.2026')[-1]
            self.assertEqual(card['kind'], 'confirmation_required')
            original_id = conversation.context['agent_run']['task_id']
            continue_after_cancellation(conversation, self.user, 'cash.transaction_create')
            state = conversation.context['agent_run']
            self.assertEqual(state['task_id'], original_id)
            self.assertEqual(state['steps'][-1]['status'], 'cancelled')
        elif mode == 'empty_search':
            self.gateway.side_effect = [
                planned_call('read:cash.transactions', steps=[
                    {'id': 'search', 'capability_id': 'read:cash.transactions', 'objective': 'Найти операцию', 'expected_result': 'Найденные операции'},
                    {'id': 'create', 'capability_id': 'cash.transaction_create', 'objective': 'Подготовить расход', 'expected_result': 'Карточка'},
                ]),
                function_call('read_cash_transactions', {'plan': {'limit': 1}}),
                function_call('assistant_ask', {'question': 'Операций не найдено. Уточните условия поиска.'}),
            ]
            respond(conversation, self.user, 'Найди прежнюю операцию и подготовь похожий расход')
            reads = [s for s in conversation.context['agent_run']['steps'] if s['tool_id'].startswith('read:')]
            self.assertEqual(reads[0]['result']['total_count'], 0)
        else:
            self.gateway.side_effect = [planned_call('cash.transaction_create'), function_call('assistant_ask', {'question': 'За что расход?'})]
            respond(conversation, self.user, 'Подготовь расход')
        self.assertTrue(conversation.context['agent_run']['waiting_user'])
        self.assertFalse(CashTransaction.objects.exists())
        return conversation

    def test_explicit_switch_starts_new_task_in_each_open_state(self):
        for mode in ('cancelled_card', 'clarification', 'empty_search'):
            with self.subTest(mode=mode):
                conversation = self.open_task(mode)
                old_id = conversation.context['agent_run']['task_id']
                snapshots = []
                responses = iter([
                    function_call('assistant_cancel', {'reason': 'Пользователь явно сменил задачу', 'answer': 'Перехожу к новой задаче.', 'new_task': True}),
                    planned_call('read:cash.transactions', goal='Показать список операций'),
                    function_call('read_cash_transactions', {'plan': {'limit': 1}}),
                    function_call('assistant_finish', {'answer': 'Операций нет.'}),
                ])
                def gateway(messages, tools):
                    snapshots.append(json.loads(json.dumps(messages)))
                    return next(responses)
                self.gateway.side_effect = gateway
                question = 'Отмени прежнюю задачу. Вместо неё покажи список операций.'
                response = respond(conversation, self.user, question)[-1]
                self.assertEqual(response['text'], 'Операций нет.')
                plans = list(conversation.feedback_events.filter(scope='assistant_debug', payload__stage='agent.plan').order_by('pk'))
                self.assertNotEqual(plans[-1].payload['output']['task_id'], old_id)
                self.assertEqual(plans[-1].payload['output']['plan']['version'], 1)
                self.assertEqual([m['content'] for m in snapshots[1] if m['role'] == 'user'], [question])
                self.assertNotIn('agent_run', conversation.context)
                self.assertFalse(CashTransaction.objects.exists())

    def test_missing_switch_can_be_accepted_as_replan_in_each_open_state(self):
        # A passing assertion reproduces the gap; it does not endorse this route.
        for mode in ('cancelled_card', 'clarification', 'empty_search'):
            with self.subTest(mode=mode):
                conversation = self.open_task(mode)
                old_id = conversation.context['agent_run']['task_id']
                version = conversation.context['agent_run']['plan']['version']
                self.gateway.side_effect = [
                    planned_call('cash.transaction_create', goal='Ответить сколько будет два плюс два, без операций',
                        revision_reason='Пользователь переключился на независимый вопрос', steps=[{
                            'id': 'answer', 'capability_id': 'cash.transaction_create',
                            'objective': 'Не выполнять операцию; ответить на арифметический вопрос', 'expected_result': 'Ответ 4',
                        }]),
                    function_call('assistant_finish', {'answer': '4'}),
                    function_call('assistant_ask', {'question': '4. Вернуться к расходу?'}),
                ]
                response = respond(conversation, self.user, 'Прежнюю задачу отменяю. Другой вопрос: сколько будет два плюс два?')[-1]
                state = conversation.context['agent_run']
                self.assertEqual(state['task_id'], old_id)
                self.assertEqual(state['plan']['version'], version + 1)
                self.assertEqual(state['active_capability'], 'cash.transaction_create')
                self.assertTrue(state['waiting_user'])
                self.assertEqual(response['text'], '4. Вернуться к расходу?')
                errors = conversation.feedback_events.filter(scope='assistant_debug', payload__stage='agent.protocol_error')
                self.assertEqual(errors.count(), 1)
                self.assertIn('Нельзя завершить задачу', errors.get().payload['error'])
                self.assertFalse(conversation.feedback_events.filter(scope='assistant_debug', payload__stage='agent.task_cancelled').exists())
                self.assertFalse(CashTransaction.objects.exists())

    def test_same_task_clarification_preserves_plan_without_cancel(self):
        conversation = self.open_task('clarification')
        state = conversation.context['agent_run']
        original_id, version = state['task_id'], state['plan']['version']
        self.gateway.side_effect = [function_call('assistant_ask', {'question': 'Какая сумма?'})]
        respond(conversation, self.user, 'За покупку бумаги')
        state = conversation.context['agent_run']
        self.assertEqual((state['task_id'], state['plan']['version']), (original_id, version))
        self.assertFalse(conversation.feedback_events.filter(scope='assistant_debug', payload__stage='agent.task_cancelled').exists())
        self.assertFalse(CashTransaction.objects.exists())
