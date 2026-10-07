from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from tenders.models import TenderSettings

from .models import FilterSettings

URL = "/tender-selection/evaluation-settings/"
VALID = {
    "risk_warning_days": "10", "risk_critical_days": "5",
    "roi_good_percent": "35", "roi_thin_percent": "20", "vat_rate": "7",
    "level_samples_required": "high", "level_samples_impossible": "high",
    "level_national_confirmation": "low", "level_national_blocked": "high",
    "level_delivery_requests": "medium", "level_delivery_open_ended": "high",
    "default_reduction_percent": "25",
    "stats_target_count": "15", "stats_min_samples": "2", "reduction_hint_min": "3", "reduction_hint_max": "50",
    "incoming_ttl_days": "10",
}


class EvaluationSettingsTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)

    def test_kanban_links_to_settings(self):
        response = self.client.get("/tender-selection/?view=kanban")
        self.assertContains(response, f'href="{URL}"')

    def test_page_shows_current_values(self):
        response = self.client.get(URL)
        self.assertContains(response, 'name="roi_good_percent" value="30')
        self.assertContains(response, 'name="risk_critical_days" value="7"')

    def test_saving_updates_risk_roi_and_vat(self):
        self.client.post(URL, VALID)

        filters = FilterSettings.load()
        tender_settings = TenderSettings.objects.get(pk=1)
        self.assertEqual((filters.risk_warning_days, filters.risk_critical_days), (10, 5))
        self.assertEqual((tender_settings.roi_good_percent, tender_settings.roi_thin_percent), (Decimal("35"), Decimal("20")))
        self.assertEqual(tender_settings.vat_rate, Decimal("7"))
        self.assertEqual(tender_settings.default_reduction_percent, Decimal("25"))
        self.assertEqual(filters.risk_factor_levels["samples_required"], "high")
        self.assertEqual(filters.risk_factor_levels["national_confirmation"], "low")
        self.assertEqual(
            (filters.stats_target_count, filters.stats_min_samples, filters.reduction_hint_min, filters.reduction_hint_max),
            (15, 2, 3, 50),
        )
        self.assertEqual(filters.incoming_ttl_days, 10)

    def test_inconsistent_thresholds_are_rejected(self):
        for broken in (
            {"risk_critical_days": "10"}, {"roi_thin_percent": "35"}, {"vat_rate": "abc"},
            {"level_samples_required": "purple"}, {"reduction_hint_min": "70"}, {"stats_min_samples": "20"},
        ):
            response = self.client.post(URL, {**VALID, **broken})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(FilterSettings.load().risk_warning_days, 14)
            self.assertFalse(TenderSettings.objects.filter(roi_good_percent=Decimal("35")).exists())

    def test_filter_settings_form_no_longer_touches_risk_days(self):
        filters = FilterSettings.load()
        filters.risk_warning_days, filters.risk_critical_days = 20, 9
        filters.save()

        self.client.post("/tender-selection/settings/", {"min_price": "300000", "window_days": "7"})

        filters.refresh_from_db()
        self.assertEqual((filters.risk_warning_days, filters.risk_critical_days), (20, 9))


class ConfiguredBehaviourTests(TestCase):
    def test_factor_levels_come_from_settings(self):
        from .risk_policy import classify_risk

        facts = {"documents_sufficient": True, "samples": "required", "national_regime": "confirmation_required"}
        result = classify_risk(facts, levels={"samples_required": "high", "national_confirmation": "low"})

        self.assertEqual(result["risk_level"], "high")
        self.assertEqual({f["code"]: f["level"] for f in result["risk_factors"]}, {"samples": "high", "national_regime": "low"})

    def test_incoming_ttl_comes_from_settings(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import services
        from .models import Tender

        filters = FilterSettings.load()
        filters.incoming_ttl_days = 3
        filters.save()
        Tender.objects.create(purchase_number="old", collecting_finished_at=timezone.now() - timedelta(days=4))
        Tender.objects.create(purchase_number="fresh", collecting_finished_at=timezone.now() - timedelta(days=2))

        services.purge_stale()

        self.assertEqual(list(Tender.objects.values_list("purchase_number", flat=True)), ["fresh"])

    def test_expired_evaluation_and_calculation_leave_the_kanban(self):
        from datetime import timedelta

        from django.utils import timezone

        from tenders.models import TenderEstimate

        from . import services
        from .models import Tender

        filters = FilterSettings.load()
        owner = get_user_model().objects.create_user("manager")
        evaluation = Tender.objects.create(
            purchase_number="expired-evaluation", review=Tender.INTERESTING,
            collecting_finished_at=timezone.now() - timedelta(minutes=1),
        )
        calculation = Tender.objects.create(
            purchase_number="expired-calculation", status=Tender.PUSHED,
            outcome_status=Tender.OUTCOME_DRAFT,
            collecting_finished_at=timezone.now() - timedelta(minutes=1),
        )
        TenderEstimate.objects.create(owner=owner, tender=calculation, tender_number=calculation.purchase_number, name="Расчёт")
        bidding = Tender.objects.create(
            purchase_number="active-bidding", status=Tender.PUSHED,
            outcome_status=Tender.OUTCOME_PENDING,
            collecting_finished_at=timezone.now() - timedelta(minutes=1),
        )

        services.purge_stale()

        evaluation.refresh_from_db()
        calculation.refresh_from_db()
        bidding.refresh_from_db()
        self.assertEqual((evaluation.status, evaluation.archived_from_stage), (Tender.DISMISSED, "evaluation"))
        self.assertEqual((calculation.status, calculation.archived_from_stage), (Tender.DISMISSED, "calculation"))
        self.assertEqual(bidding.status, Tender.PUSHED)

    def test_forecast_uses_configured_sample_size_and_hint_bounds(self):
        from .models import ContractStat
        from .models import Tender
        from .stats import price_stats_for

        ContractStat.objects.create(
            law="fz44", purchase_number="1", subject="Футболки хлопковые", discount_pct=Decimal("70"),
            own_funnel=True,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples, filters.reduction_hint_max = 1, 50
        filters.save()

        stats = price_stats_for(Tender(purchase_number="2", title="Поставка футболки хлопковые"))

        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["suggested_reduction"], 50)

    def test_market_scan_rows_do_not_count_toward_the_forecast(self):
        """ContractStat также наполняется отдельным сканом рынка по категориям
        (collect_price_stats, own_funnel=False) — в прогноз идёт только своя
        воронка (own_funnel=True), рынок не подмешиваем."""
        from .models import ContractStat, Tender
        from .stats import price_stats_for

        ContractStat.objects.create(
            law="fz44", purchase_number="99", subject="Футболки хлопковые", discount_pct=Decimal("0"),
            own_funnel=False,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples = 1
        filters.save()

        stats = price_stats_for(Tender(purchase_number="2", title="Поставка футболки хлопковые"))

        self.assertIsNone(stats)

    def test_legacy_funnel_row_without_category_is_matched_by_subject(self):
        from .models import ContractStat, Tender
        from .stats import price_stats_for

        ContractStat.objects.create(
            law="fz44", purchase_number="1", subject="Футболки хлопковые", discount_pct=Decimal("20"),
            own_funnel=True,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples = 1
        filters.save()

        stats = price_stats_for(Tender(
            purchase_number="2", title="Поставка футболки хлопковые", okpd2=["17.23"],
        ))

        self.assertEqual(stats["count"], 1)

    def test_excluded_funnel_row_does_not_count_toward_forecast(self):
        from .models import ContractStat, Tender
        from .stats import price_stats_for

        ContractStat.objects.create(
            law="fz44", purchase_number="1", subject="Футболки хлопковые", discount_pct=Decimal("20"),
            own_funnel=True, forecast_included=False,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples = 1
        filters.save()

        self.assertIsNone(price_stats_for(Tender(purchase_number="2", title="Поставка футболки хлопковые")))

    def test_insufficient_history_reports_why_via_diag_instead_of_vanishing(self):
        """Когда своей истории мало — карточка должна показать «пока нет данных»,
        а не молча спрятать блок целиком (иначе выглядит как баг)."""
        from .models import ContractStat, Tender
        from .stats import price_stats_for

        ContractStat.objects.create(
            law="fz44", purchase_number="1", subject="Футболки хлопковые", discount_pct=Decimal("70"),
            own_funnel=True,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples = 3
        filters.save()

        diag = {}
        stats = price_stats_for(Tender(purchase_number="2", title="Поставка футболки хлопковые"), diag=diag)

        self.assertIsNone(stats)
        self.assertEqual(diag, {"count": 1, "min_samples": 3})

    def test_new_estimate_without_forecast_uses_default_reduction(self):
        from .models import Tender
        from .services import push_to_estimate

        tender_settings = TenderSettings.objects.get_or_create(pk=1)[0]
        tender_settings.default_reduction_percent = Decimal("22")
        tender_settings.save()
        user = get_user_model().objects.create_user("manager")
        tender = Tender.objects.create(purchase_number="1", title="Кружки")

        from tenders.models import TenderEstimate

        estimate = TenderEstimate.objects.get(pk=push_to_estimate(tender, user))
        self.assertEqual(estimate.reduction_percent, Decimal("22.00"))

    def test_market_forecast_survives_manual_calculation_change(self):
        from .models import ContractStat, Tender
        from .services import push_to_estimate

        ContractStat.objects.create(
            law="fz44", purchase_number="past", contract_reg_num="past", subject="Поставка кружек", discount_pct=Decimal("13"),
            own_funnel=True,
        )
        filters = FilterSettings.load()
        filters.stats_min_samples = 1
        filters.save()
        user = get_user_model().objects.create_user("manager")
        tender = Tender.objects.create(purchase_number="new", title="Поставка кружек")

        from tenders.models import TenderEstimate

        estimate = TenderEstimate.objects.get(pk=push_to_estimate(tender, user))
        estimate.reduction_percent = Decimal("30")
        estimate.save(update_fields=["reduction_percent"])
        tender.refresh_from_db()

        self.assertEqual(tender.market_forecast_percent, Decimal("13"))
        self.assertEqual(tender.market_forecast_sample_count, 1)

    def test_current_tender_can_be_excluded_from_its_live_forecast(self):
        from .models import ContractStat, Tender
        from .stats import price_stats_for

        ContractStat.objects.create(law="fz44", purchase_number="past", contract_reg_num="past", subject="Поставка кружек", discount_pct=Decimal("13"), own_funnel=True)
        ContractStat.objects.create(law="fz44", purchase_number="current", contract_reg_num="current", subject="Поставка кружек", discount_pct=Decimal("24"), own_funnel=True)
        filters = FilterSettings.load()
        filters.stats_min_samples = 1
        filters.save()

        stats = price_stats_for(Tender(purchase_number="current", title="Поставка кружек"), exclude_purchase_number="current")

        self.assertEqual(stats["median"], 13)
