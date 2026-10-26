"""Characterise task switching without pretending scripted calls prove model reliability."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from cash.models import CashTransaction
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
        if mode == 'empty_search':
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
        for mode in ('clarification', 'empty_search'):
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
                users = [m['content'] for m in snapshots[1] if m['role'] == 'user']
                self.assertEqual(users[-1], question)
                self.assertEqual(len(users), 2)
                self.assertFalse(any(m.get('tool_calls') or m['role'] == 'tool' for m in snapshots[1]))
                self.assertNotIn('agent_run', conversation.context)
                self.assertFalse(CashTransaction.objects.exists())

    def test_missing_switch_can_be_accepted_as_replan_in_each_open_state(self):
        # A passing assertion reproduces the gap; it does not endorse this route.
        for mode in ('clarification', 'empty_search'):
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

    def cancel_task(self):
        from django.urls import reverse
        self.client.force_login(self.user)
        conversation = create_conversation(self.user)
        self.gateway.side_effect = [planned_call('cash.transaction_create'), function_call('cash_transaction_create', {
            'operation_date': '2026-10-25', 'account': 'cash', 'direction': 'expense',
            'amount': 260, 'reason': 'Покупка коробок',
        })]
        card = respond(conversation, self.user, 'Подготовь расход наличными 260 рублей за покупку коробок на 25.10.2026')[-1]
        original_id = conversation.context['agent_run']['task_id']
        before_calls = self.gateway.call_count
        result = self.client.post(reverse('assistant_conversation_action_confirm', args=[conversation.pk, card['id']]), {'accept': 'no'})
        self.assertEqual(result.status_code, 200)
        conversation.refresh_from_db()
        self.assertNotIn('agent_run', conversation.context)
        self.assertEqual(self.gateway.call_count, before_calls)
        self.assertEqual(conversation.context['agent_memory']['active_task']['status'], 'cancelled')
        self.assertFalse(CashTransaction.objects.exists())
        return conversation, original_id

    def test_cancelled_task_amount_change_uses_user_history_but_needs_fresh_confirmation(self):
        conversation, original_id = self.cancel_task()
        self.gateway.side_effect = [
            planned_call('cash.transaction_create', goal='Подготовить расход на 600 рублей'),
            function_call('cash_transaction_create', {'operation_date': '2026-10-25', 'account': 'cash',
                'direction': 'expense', 'amount': 600, 'reason': 'Покупка коробок'}),
        ]
        result = respond(conversation, self.user, 'Измени сумму на 600 рублей')[-1]
        state = conversation.context['agent_run']
        self.assertNotEqual(state['task_id'], original_id)
        self.assertEqual(state['plan']['version'], 1)
        self.assertEqual(state['status'], 'WAITING_CONFIRMATION')
        self.assertEqual(result['kind'], 'confirmation_required')
        self.assertEqual(result['data']['arguments']['amount'], 600)
        self.assertEqual(result['data']['arguments']['reason'], 'Покупка коробок')
        self.assertEqual([m['content'] for m in state['messages'] if m['role'] == 'user'], [
            'Подготовь расход наличными 260 рублей за покупку коробок на 25.10.2026', 'Измени сумму на 600 рублей'])
        self.assertFalse(any(step['tool_id'] == 'cash.transaction_create' for step in state['steps']))
        self.assertFalse(CashTransaction.objects.exists())

    def test_image_after_button_cancel_has_no_old_execution_steps(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .assistant_conversations import upload_attachment
        from .test_assistant_attachments import document_bytes
        conversation, _old_id = self.cancel_task()
        image = upload_attachment(conversation, self.user, SimpleUploadedFile('image.png', document_bytes('png', '')))
        snapshots = []
        def gateway(messages, tools):
            snapshots.append(json.loads(json.dumps(messages)))
            return function_call('assistant_chat', {'answer': 'Ответ по изображению.'})
        self.gateway.side_effect = gateway
        result = respond(conversation, self.user, 'Что на картинке?', attachments=[image])[-1]
        self.assertEqual(result['data']['steps'], [])
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(any(m['role'] == 'tool' for m in snapshots[0]))
        users = [m for m in snapshots[0] if m['role'] == 'user']
        self.assertEqual(len(users), 2)
        self.assertTrue(any(p['type'] == 'image_url' for p in users[-1]['content']))
        self.assertNotIn('agent_run', conversation.context)
        self.assertFalse(CashTransaction.objects.exists())

    def test_button_cancel_preserves_completed_action_without_repeating_it(self):
        from django.urls import reverse
        self.client.force_login(self.user)
        conversation = create_conversation(self.user)
        first = {'operation_date': '2026-10-25', 'account': 'cash', 'direction': 'expense', 'amount': 10, 'reason': 'Покупка бумаги'}
        second = {**first, 'amount': 20, 'reason': 'Покупка коробок'}
        self.gateway.side_effect = [
            planned_call('cash.transaction_create', steps=[
                {'id': 'first', 'capability_id': 'cash.transaction_create', 'objective': 'Расход за бумагу', 'expected_result': 'Расход записан'},
                {'id': 'second', 'capability_id': 'cash.transaction_create', 'objective': 'Расход за коробки', 'expected_result': 'Расход записан'},
            ]),
            function_call('cash_transaction_create', first),
            function_call('cash_transaction_create', second),
        ]
        first_card = respond(conversation, self.user, 'Запиши два расхода: 10 рублей за покупку бумаги и 20 за покупку коробок, наличные, 25.10.2026')[-1]
        url = reverse('assistant_conversation_action_confirm', args=[conversation.pk, first_card['id']])
        response = self.client.post(url, {'accept': 'yes'})
        self.assertEqual(response.status_code, 200)
        conversation.refresh_from_db()
        second_card = response.json()['messages'][-1]
        self.assertEqual(second_card['kind'], 'confirmation_required')
        old_id = conversation.context['agent_run']['task_id']
        calls_before_cancel = self.gateway.call_count
        response = self.client.post(reverse('assistant_conversation_action_confirm', args=[conversation.pk, second_card['id']]), {'accept': 'no'})
        self.assertEqual(response.status_code, 200)
        conversation.refresh_from_db()
        self.assertNotIn('agent_run', conversation.context)
        self.assertEqual(self.gateway.call_count, calls_before_cancel)
        self.assertEqual([step['status'] for step in response.json()['messages'][-1]['data']['steps'] if step['tool_id'] == 'cash.transaction_create'], ['completed', 'cancelled'])
        self.assertEqual(CashTransaction.objects.count(), 1)
        snapshots = []
        replies = iter([
            function_call('assistant_chat', {'answer': '4'}),
        ])
        def gateway(messages, tools):
            snapshots.append(json.loads(json.dumps(messages)))
            return next(replies)
        self.gateway.side_effect = gateway
        self.assertEqual(respond(conversation, self.user, 'Другой вопрос: сколько два плюс два?')[-1]['text'], '4')
        self.assertEqual(CashTransaction.objects.count(), 1)
        self.assertEqual(CashTransaction.objects.get().reason, 'Покупка бумаги')
        self.assertNotIn('agent_run', conversation.context)
        actions = conversation.context['agent_memory']['recent_actions']
        self.assertEqual(len(actions), 1)
        self.assertEqual(conversation.context['agent_memory']['active_task']['status'], 'cancelled')
        self.assertEqual([m['content'] for m in snapshots[0] if m['role'] == 'user'], [
            'Запиши два расхода: 10 рублей за покупку бумаги и 20 за покупку коробок, наличные, 25.10.2026',
            'Другой вопрос: сколько два плюс два?'])
        self.assertFalse(any(m['role'] == 'tool' for m in snapshots[0]))
        self.assertEqual(conversation.feedback_events.get(scope='assistant_debug', payload__stage='agent.task_cancelled').payload['output']['task_id'], old_id)

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
