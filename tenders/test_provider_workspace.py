from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ProcessDefinition
from .provider_knowledge import confirm_knowledge, create_knowledge_draft, create_provider, initialize_structured_rules_binding
from .sewing_price_list import canonical_data_from_rows


class ProviderWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("workspace-owner", "workspace@example.test", "password")
        self.client.force_login(self.user)
        self.stage, _ = ProcessDefinition.objects.get_or_create(name="Пошив", role=ProcessDefinition.ROLE_PRODUCTION)
        self.provider, _ = create_provider(self.user, "Атекс", [self.stage], extracted_text="Пошив.xls")
        data = canonical_data_from_rows([["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"], ["Футболка", "Классическая женская", "Кулирка", 100, "", 414.8]])
        data.update(requires_confirmation=False, formula_status="confirmed")
        data["pricing"]["currency"] = "RUB"
        version = confirm_knowledge(create_knowledge_draft(self.provider, self.user, data, stage=self.stage), self.user)
        self.binding = initialize_structured_rules_binding(version)

    def test_workspace_has_tabs_and_confirmed_pricing_summary(self):
        response = self.client.get(reverse("provider_workspace", args=[self.provider.pk]), {"tab": "prices"}, secure=True)
        self.assertContains(response, 'data-provider-tab="overview"')
        self.assertContains(response, "Версия 1")
        self.assertContains(response, "1 вариант")

    def test_provider_list_opens_provider_in_shared_workspace(self):
        response = self.client.get(reverse("provider_list"), secure=True)
        self.assertContains(response, "data-workspace-open")
        self.assertContains(response, 'href="%s"' % reverse("provider_detail", args=[self.provider.pk]))

    def test_workspace_calculator_uses_provider_service(self):
        response = self.client.post(
            reverse("provider_workspace", args=[self.provider.pk]),
            {"tab": "calculator", "binding_id": self.binding.pk, "variant": "Футболка | Классическая женская | Кулирка", "quantity": "100"},
            secure=True,
        )
        self.assertContains(response, "414,80 ₽")
        self.assertContains(response, "41 480,00 ₽")

    def test_draft_price_can_be_deleted(self):
        draft = create_knowledge_draft(self.provider, self.user, {"pricing": {}}, stage=self.stage)

        self.client.post(
            reverse("provider_workspace", args=[self.provider.pk]),
            {"tab": "prices", "remove_version_id": draft.pk},
            secure=True,
        )

        self.assertFalse(type(draft).objects.filter(pk=draft.pk).exists())

    def test_old_price_can_be_hidden_from_history(self):
        old = create_knowledge_draft(self.provider, self.user, {"pricing": {}}, stage=self.stage)
        old.status = old.STATUS_SUPERSEDED
        old.save(update_fields=["status"])

        response = self.client.post(
            reverse("provider_workspace", args=[self.provider.pk]),
            {"tab": "prices", "remove_version_id": old.pk},
            secure=True,
        )

        old.refresh_from_db()
        self.assertTrue(old.source_metadata["hidden"])
        self.assertNotContains(response, f"Версия {old.version_number}")

