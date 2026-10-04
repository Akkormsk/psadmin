from django.contrib.auth import get_user_model
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tenders.models import TenderEstimate

from .models import Tender


class LayeredWorkspaceNavigationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("workspace-admin", password="x")
        self.client.force_login(self.admin)

    def test_kanban_opens_tender_in_shared_workspace_layer(self):
        tender = Tender.objects.create(
            purchase_number="42",
            title="Услуги печати — тест многослойной навигации",
            review=Tender.INTERESTING,
            max_price=Decimal("500000"),
            collecting_finished_at=timezone.now() + timedelta(days=5),
        )

        response = self.client.get(f"{reverse('tender_selection:list')}?view=kanban")

        self.assertContains(response, 'id="app-workspace-stack"')
        self.assertContains(response, 'data-workspace-open')
        self.assertContains(response, "const browserHistory = window.history;")
        self.assertContains(response, "removeToDepth(layers.length - 1);")
        self.assertContains(response, "if (reloadPage) window.location.reload();")
        self.assertContains(response, reverse("tender_selection:detail", args=[tender.pk]))
        self.assertContains(response, "let workspaceApi = null;")
        self.assertNotContains(response, "window.PSWorkspace")

    def test_embedded_tender_opens_calculation_as_next_layer(self):
        tender = Tender.objects.create(
            purchase_number="43",
            title="Тендер с расчётом",
            review=Tender.INTERESTING,
            status=Tender.PUSHED,
            notification_raw={"source": {}},
        )
        estimate = TenderEstimate.objects.create(
            owner=self.admin,
            tender=tender,
            tender_number=tender.purchase_number,
            name="Расчёт тендера",
        )

        response = self.client.get(
            f"{reverse('tender_selection:detail', args=[tender.pk])}?workspace=1"
        )

        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(response, '<body class="has-account-bar workspace-embedded ">', html=False)
        self.assertContains(response, 'data-workspace-open')
        self.assertContains(response, 'data-workspace-dismiss')
        self.assertContains(response, reverse("tender_pipeline_estimate", args=[estimate.pk]))
        self.assertContains(response, 'data-workspace-title="Расчёт"')
        self.assertContains(response, "ps-workspace-open")

    def test_direct_tender_link_remains_a_normal_page(self):
        tender = Tender.objects.create(
            purchase_number="44",
            title="Обычная карточка",
            review=Tender.INTERESTING,
            notification_raw={"source": {}},
        )

        response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]))

        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertContains(response, '<body class="has-account-bar ">', html=False)
