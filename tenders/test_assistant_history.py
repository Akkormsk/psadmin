from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .assistant_conversations import add_assistant_message, create_conversation
from .models import OwnerInteraction


class AssistantHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('history-owner')
        self.client.force_login(self.user)

    def test_all_saved_conversations_are_reachable_in_pages_of_thirty(self):
        saved = [create_conversation(self.user, f'Беседа {index}') for index in range(65)]
        add_assistant_message(saved[0], 'Самый старый ответ')
        before = {item.pk: item.context for item in saved}
        other = get_user_model().objects.create_user('history-other')
        foreign = create_conversation(other, 'Чужая беседа')
        identifiers = []
        page = 1
        while page:
            data = self.client.get(reverse('assistant_conversations'), {'page': page}).json()
            self.assertLessEqual(len(data['conversations']), 30)
            identifiers.extend(item['id'] for item in data['conversations'])
            page = data['next_page']
        self.assertEqual(identifiers, [item.pk for item in reversed(saved)])
        self.assertNotIn(foreign.pk, identifiers)
        self.assertEqual(dict(OwnerInteraction.objects.filter(pk__in=before).values_list('pk', 'context')), before)
        detail = self.client.get(reverse('assistant_conversation_detail', args=[saved[0].pk])).json()
        self.assertEqual(detail['messages'][0]['text'], 'Самый старый ответ')
        self.assertEqual(self.client.get(reverse('assistant_conversation_detail', args=[foreign.pk])).status_code, 404)

    def test_listing_does_not_create_and_explicit_new_creates_once(self):
        self.client.get(reverse('assistant_conversations'))
        self.assertFalse(OwnerInteraction.objects.exists())
        response = self.client.post(reverse('assistant_conversation_new'), {'title': 'Новая беседа'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(OwnerInteraction.objects.count(), 1)
        self.assertEqual(self.client.get(reverse('assistant_conversations')).json()['next_page'], None)
