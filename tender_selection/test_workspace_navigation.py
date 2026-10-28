from django.contrib.auth import get_user_model
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from tenders.models import TenderEstimate

from .models import Tender


@override_settings(SECURE_SSL_REDIRECT=False)
class LayeredWorkspaceNavigationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("workspace-admin", password="x")
        self.client.force_login(self.admin)
        refresh = patch("tender_selection.views.start_extras_refresh_in_background")
        refresh.start()
        self.addCleanup(refresh.stop)

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
        self.assertNotContains(response, 'app-workspace-layer__external')
        self.assertContains(response, 'data-navigation-home')
        self.assertNotContains(response, 'account-bar__home')
        self.assertNotContains(response, 'account-bar__data')
        self.assertContains(response, "function activeWorkspaceContext()")
        self.assertContains(response, "function renderCurrentContext()")
        self.assertContains(response, 'window.addEventListener("ps-workspace-context"')
        self.assertContains(response, 'type: "ps-workspace-context"')
        self.assertContains(response, 'window.addEventListener("load", reportWorkspaceContext);')
        self.assertContains(response, 'const localBack = document.querySelector(".container > .back-link:first-child");')
        self.assertNotContains(response, "window.PSWorkspace")

    def test_embedded_tender_opens_calculation_as_next_layer(self):
        tender = Tender.objects.create(
            purchase_number="43",
            title="Тендер с расчётом",
            review=Tender.INTERESTING,
            status=Tender.PUSHED,
            notification_raw={"source": {}},
            extras_checked_at=timezone.now(),
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
        self.assertContains(response, 'data-tender-transition')
        self.assertContains(response, reverse("tender_pipeline_estimate", args=[estimate.pk]))
        self.assertContains(response, 'data-workspace-title="Расчёт"')
        self.assertContains(response, "ps-workspace-open")
        self.assertContains(response, 'class="container ts-detail ts-detail--document"')
        self.assertContains(response, 'class="workspace-action-stack"')

    def test_embedded_workspace_does_not_render_a_second_assistant(self):
        tender = Tender.objects.create(
            purchase_number="assistant-shell",
            title="Тендер без второго ассистента",
            notification_raw={"source": {}},
        )

        response = self.client.get(
            f"{reverse('tender_selection:detail', args=[tender.pk])}?workspace=1"
        )

        self.assertNotContains(response, 'id="assistant-launcher"')
        direct_response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]))
        self.assertContains(direct_response, 'id="assistant-launcher"')

    def test_embedded_dismiss_refreshes_the_parent_collection(self):
        tender = Tender.objects.create(
            purchase_number="dismiss-fast",
            title="Быстро скрываемый тендер",
            notification_raw={"source": {}},
        )

        with patch("tender_selection.views.start_extras_refresh_in_background") as refresh:
            response = self.client.get(
                f"{reverse('tender_selection:detail', args=[tender.pk])}?workspace=1"
            )

        refresh.assert_called_once_with(tender.pk, force=False)
        self.assertContains(response, "data-tender-transition")
        self.assertContains(response, "ps-tender-transition")
        self.assertNotContains(response, "removeTenderCard")

    def test_embedded_stage_change_uses_the_shared_transition_handler(self):
        tender = Tender.objects.create(
            purchase_number="move-fast",
            title="Тендер для перехода",
            notification_raw={"source": {}},
        )

        with patch("tender_selection.views.start_extras_refresh_in_background"):
            response = self.client.get(
                f"{reverse('tender_selection:detail', args=[tender.pk])}?workspace=1"
            )

        self.assertContains(response, "data-tender-transition")
        self.assertContains(response, "ps-tender-transition")

    def test_direct_tender_link_remains_a_normal_page(self):
        tender = Tender.objects.create(
            purchase_number="44",
            title="Обычная карточка",
            review=Tender.INTERESTING,
            notification_raw={"source": {}},
            extras_checked_at=timezone.now(),
        )

        response = self.client.get(reverse("tender_selection:detail", args=[tender.pk]))

        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertContains(response, '<body class="has-account-bar ">', html=False)


class WorkspaceRedirectTests(TestCase):
    def test_internal_post_redirect_keeps_embedding_query_and_fragment(self):
        from django.http import HttpResponseRedirect
        from django.test import RequestFactory
        from mysite.middleware import WorkspaceFrameOptionsMiddleware
        middleware = WorkspaceFrameOptionsMiddleware(lambda request: HttpResponseRedirect('/tender-selection/42/?from=archive#tender-items'))
        response = middleware(RequestFactory().post('/action/?workspace=1'))
        self.assertEqual(response['Location'], '/tender-selection/42/?from=archive&workspace=1#tender-items')
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')

    def test_direct_and_external_redirects_are_not_changed(self):
        from django.http import HttpResponseRedirect
        from django.test import RequestFactory
        from mysite.middleware import WorkspaceFrameOptionsMiddleware
        for source, target in [('/action/', '/tender-selection/42/'), ('/action/?workspace=1', 'https://external.test/path')]:
            response = WorkspaceFrameOptionsMiddleware(lambda request: HttpResponseRedirect(target))(RequestFactory().post(source))
            self.assertEqual(response['Location'], target)

    def test_existing_workspace_query_is_not_duplicated(self):
        from django.http import HttpResponseRedirect
        from django.test import RequestFactory
        from mysite.middleware import WorkspaceFrameOptionsMiddleware
        response = WorkspaceFrameOptionsMiddleware(lambda request: HttpResponseRedirect('/card/?workspace=1&filter='))(RequestFactory().post('/action/?workspace=1'))
        self.assertEqual(response['Location'], '/card/?filter=&workspace=1')
