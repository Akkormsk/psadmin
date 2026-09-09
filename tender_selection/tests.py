from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import gosplan
from .filtering import match_title, parse_terms
from .models import FilterSettings, FoundTender, Organization, PullRun
from .services import run_pull


class CleanTitleTests(TestCase):
    def test_strips_eis_prefix(self):
        self.assertEqual(
            gosplan.clean_title("2026-06422**Техническое обслуживание медтехники"),
            "Техническое обслуживание медтехники",
        )

    def test_strips_single_asterisk_prefix(self):
        self.assertEqual(
            gosplan.clean_title("2026-06393*Поставка лекарственного препарата"),
            "Поставка лекарственного препарата",
        )

    def test_leaves_plain_title_untouched(self):
        self.assertEqual(gosplan.clean_title("Поставка кружек"), "Поставка кружек")


class EisUrlTests(TestCase):
    def test_known_type_maps_to_notice_segment(self):
        url = gosplan.eis_url("0131200001026005999", "epNotificationEF2020")
        self.assertIn("/notice/ea44/view/common-info.html?regNumber=0131200001026005999", url)

    def test_unknown_type_falls_back_to_search(self):
        url = gosplan.eis_url("123", "somethingElse")
        self.assertIn("extendedsearch/results.html?searchString=123", url)


class RunPullTests(TestCase):
    RECORD = {
        "purchase_number": "0131200001026005999",
        "object_info": "2026-06422**Поставка сувенирной продукции",
        "max_price": "377156.06",
        "currency_code": "RUB",
        "customers": ["3666018060"],
        "okpd2": ["32.99.12"],
        "region": 36,
        "stage": 1,
        "purchase_type": "epNotificationEF2020",
        "published_at": "2026-09-07T14:59:35.309000",
        "collecting_finished_at": "2026-09-16T06:00:00",
    }

    def test_creates_then_updates_and_logs(self):
        with mock.patch.object(gosplan, "iter_purchases", return_value=iter([self.RECORD])), \
             mock.patch("tender_selection.services.enrich_organizations", return_value=0):
            run = run_pull(days=3, max_requests=1)
        self.assertTrue(run.ok)
        self.assertEqual(run.created_count, 1)
        tender = FoundTender.objects.get(purchase_number="0131200001026005999")
        self.assertEqual(tender.title, "Поставка сувенирной продукции")
        self.assertEqual(tender.customer_inn, "3666018060")
        self.assertIn("ea44", tender.eis_url)

        with mock.patch.object(gosplan, "iter_purchases", return_value=iter([self.RECORD])), \
             mock.patch("tender_selection.services.enrich_organizations", return_value=0):
            run2 = run_pull(days=3, max_requests=1)
        self.assertEqual(run2.created_count, 0)
        self.assertEqual(run2.updated_count, 1)
        self.assertEqual(FoundTender.objects.count(), 1)

    def test_api_error_is_recorded_not_raised(self):
        with mock.patch.object(gosplan, "iter_purchases", side_effect=gosplan.GosplanError("HTTP 429")):
            run = run_pull(days=3, max_requests=1)
        self.assertFalse(run.ok)
        self.assertIn("429", run.error)


class MultiSourceTests(TestCase):
    def test_fz223_record_shape(self):
        from tender_selection.services import _record_to_fields
        rec = {
            "purchase_number": "31900000001", "object_info": "Поставка сувенирной продукции",
            "customer": "7700000000", "submission_close_at": "2026-09-20T09:00:00",
            "max_price": "500000", "region": 77, "okpd2": ["32.99.11"],
        }
        f = _record_to_fields(rec, timezone.now(), "fz223")
        self.assertEqual(f["customer_inn"], "7700000000")
        self.assertIsNotNone(f["collecting_finished_at"])
        self.assertIn("223/purchase", f["eis_url"])

    def test_223_org_normalization(self):
        from tender_selection.services import _org_fields
        src = {"mainInfo": {"fullName": 'ПАО "РОССЕТИ ЦЕНТР"', "shortName": 'ПАО "РОССЕТИ ЦЕНТР"',
                            "postalAddress": "г. Москва, ул. Ордынка", "region": "Г.МОСКВА"},
               "contactInfo": {"contactEmail": "x@mrsk.ru", "website": "mrsk-1.ru"}}
        f = _org_fields(src, "fz223")
        self.assertIn("РОССЕТИ ЦЕНТР", f["name"])
        self.assertEqual(f["email"], "x@mrsk.ru")
        self.assertEqual(f["region_name"], "Г.Москва")

    def test_list_law_filter(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser("a", "a@e.ru", "p"))
        FilterSettings.objects.update_or_create(pk=1, defaults={"min_price": 0})
        FoundTender.objects.create(purchase_number="1", law="fz44", object_info="A", title="Кружка 44", last_pulled_at=timezone.now())
        FoundTender.objects.create(purchase_number="2", law="fz223", object_info="B", title="Кружка 223", last_pulled_at=timezone.now())
        r44 = self.client.get(reverse("tender_selection:list") + "?law=fz44")
        self.assertContains(r44, "Кружка 44")
        self.assertNotContains(r44, "Кружка 223")
        rboth = self.client.get(reverse("tender_selection:list"))
        self.assertContains(rboth, "Кружка 44")
        self.assertContains(rboth, "Кружка 223")

    def test_settings_save_categories_regions_laws(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser("a", "a@e.ru", "p"))
        self.client.post(reverse("tender_selection:settings"), {
            "include_words": "", "exclude_words": "", "min_price": "300000", "window_days": "7",
            "okpd2": ["18.1", "32.99"], "region": ["77", "78"], "law": ["fz44", "fz223"],
        })
        s = FilterSettings.load()
        self.assertEqual(sorted(s.okpd2_codes), ["18.1", "32.99"])
        self.assertEqual(sorted(s.regions), ["77", "78"])
        self.assertEqual(sorted(s.laws), ["fz223", "fz44"])


class PushToEstimateTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@e.ru", "pw")
        self.client.force_login(self.admin)
        for name in ("fetch_clarifications", "fetch_complaints"):
            patcher = mock.patch.object(gosplan, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tender = FoundTender.objects.create(
            purchase_number="1", law="fz44", object_info="x", title="Сувенирка",
            max_price=500000, last_pulled_at=timezone.now(),
            notification_raw=NOTIFICATION_FIXTURE,
        )

    def test_creates_estimate_with_lines_from_notification(self):
        resp = self.client.post(reverse("tender_selection:push", args=[self.tender.pk]))
        from tenders.models import TenderEstimate
        est = TenderEstimate.objects.get()
        self.assertEqual(est.tender_number, "1")
        self.assertIn("ПРИМЕР", est.name)
        self.assertNotIn("№", est.name)
        self.assertEqual(est.lines.count(), 1)
        line = est.lines.first()
        self.assertEqual(line.name, "Баннер")
        self.assertEqual(str(line.nmck_unit), "3700.00")
        reqs = line.requirements["requirements"]
        self.assertTrue(any("баннерная ткань" in r["value"] for r in reqs))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, FoundTender.PUSHED)
        self.assertEqual(self.tender.pushed_estimate_id, est.pk)
        self.assertRedirects(resp, f"/tenders/{est.pk}/", fetch_redirect_response=False)

    def test_pushed_tender_shows_pill_not_review(self):
        self.client.post(reverse("tender_selection:push", args=[self.tender.pk]))
        list_resp = self.client.get(reverse("tender_selection:list"))
        self.assertContains(list_resp, "ts-onestimate-pill")
        self.assertContains(list_resp, "На расчёте")
        detail_resp = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertContains(detail_resp, "На расчёте — открыть просчёт")
        self.assertNotContains(detail_resp, 'name="review"')  # селектор статуса скрыт

    def test_second_push_opens_existing(self):
        self.client.post(reverse("tender_selection:push", args=[self.tender.pk]))
        from tenders.models import TenderEstimate
        first = TenderEstimate.objects.get().pk
        self.client.post(reverse("tender_selection:push", args=[self.tender.pk]))
        self.assertEqual(TenderEstimate.objects.count(), 1)


class DocumentPreviewTests(TestCase):
    def _docx_bytes(self):
        import io
        from docx import Document
        d = Document()
        d.add_paragraph("Описание объекта закупки")
        t = d.add_table(rows=2, cols=2)
        t.rows[0].cells[0].text = "Параметр"
        t.rows[0].cells[1].text = "Значение"
        t.rows[1].cells[0].text = "Материал"
        t.rows[1].cells[1].text = "хлопок 100%"
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue()

    def _xlsx_bytes(self):
        import io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Позиция", "Кол-во", "Цена"])
        ws.append(["Кружка", 100, 250])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_extract_docx(self):
        from .documents import extract_preview
        r = extract_preview(self._docx_bytes(), "ООЗ.docx")
        self.assertEqual(r["kind"], "docx")
        self.assertIn("Описание объекта закупки", r["html"])
        self.assertIn("хлопок 100%", r["html"])
        self.assertIn("ts-doc-table", r["html"])

    def test_extract_xlsx(self):
        from .documents import extract_preview
        r = extract_preview(self._xlsx_bytes(), "НМЦК.xlsx")
        self.assertEqual(r["kind"], "xlsx")
        self.assertIn("Кружка", r["html"])
        self.assertIn("<table", r["html"])

    def test_extract_rejects_unknown(self):
        from .documents import extract_preview
        r = extract_preview(b"random bytes", "notes.txt")
        self.assertIn("error", r)

    def test_fetch_document_blocks_foreign_url(self):
        from .documents import DocumentError, fetch_document
        with self.assertRaises(DocumentError):
            fetch_document("https://evil.example/x.docx")

    def test_view_fetches_extracts_and_caches(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser("a", "a@e.ru", "p"))
        tender = FoundTender.objects.create(
            purchase_number="1", object_info="x", title="T", last_pulled_at=timezone.now(),
            notification_raw=NOTIFICATION_FIXTURE,
        )
        with mock.patch("tender_selection.views.fetch_document", return_value=self._docx_bytes()) as f:
            resp = self.client.get(reverse("tender_selection:doc_preview", args=[tender.pk, 0]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Описание объекта закупки", resp.json()["html"])
        f.assert_called_once()
        # second call served from cache — no fetch
        with mock.patch("tender_selection.views.fetch_document") as f2:
            self.client.get(reverse("tender_selection:doc_preview", args=[tender.pk, 0]))
        f2.assert_not_called()

    def test_view_reports_fetch_error(self):
        from .documents import DocumentError
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser("a", "a@e.ru", "p"))
        tender = FoundTender.objects.create(
            purchase_number="1", object_info="x", title="T", last_pulled_at=timezone.now(),
            notification_raw=NOTIFICATION_FIXTURE,
        )
        with mock.patch("tender_selection.views.fetch_document", side_effect=DocumentError("ЕИС недоступен")):
            resp = self.client.get(reverse("tender_selection:doc_preview", args=[tender.pk, 0]))
        self.assertEqual(resp.json()["error"], "ЕИС недоступен")
        from .models import DocumentPreview
        self.assertFalse(DocumentPreview.objects.exists())  # сетевой сбой не кэшируется


class PriceStatsCollectorTests(TestCase):
    CONTRACT = {
        "purchase_number": "0111",
        "reg_num": "R1",
        "price": "168300.00",
        "okpd2": [],
        "ktru": ["17.23.13.196-00000001"],
        "region": 77,
        "subject": "Поставка канцелярских товаров",
        "suppliers": ["7712345678"],
        "published_at": "2026-09-01T10:00:00",
        "exe_start": "2026-09-05",
    }

    def test_category_from_codes(self):
        from tender_selection.stats import category_for_codes
        self.assertEqual(category_for_codes([], ["17.23.13.196-00000001"]), "17.23")
        self.assertEqual(category_for_codes(["32.99.53.190"], []), "32.99")
        self.assertEqual(category_for_codes(["99.00.00"], []), "")

    def test_collect_creates_row_then_fills_nmck_and_discount(self):
        from tender_selection.models import ContractStat
        from tender_selection.stats import collect_price_stats
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter([self.CONTRACT])), \
             mock.patch.object(gosplan, "fetch_purchase", return_value={"max_price": "210375.00"}), \
             mock.patch("tender_selection.stats.time.sleep"):
            run = collect_price_stats(since_days=30, catalog_requests=4, nmck_requests=6, categories=["17.23"])
        self.assertTrue(run.ok)
        self.assertEqual(run.created_count, 1)
        self.assertEqual(run.filled_count, 1)
        stat = ContractStat.objects.get(purchase_number="0111")
        self.assertEqual(stat.category, "17.23")
        self.assertEqual(str(stat.final_price), "168300.00")
        self.assertEqual(str(stat.nmck), "210375.00")
        self.assertEqual(str(stat.discount_pct), "20.0")
        self.assertEqual(stat.winner_inn, "7712345678")
        self.assertTrue(stat.nmck_checked)

    def test_second_run_does_not_refetch_nmck(self):
        from tender_selection.stats import collect_price_stats
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter([self.CONTRACT])), \
             mock.patch.object(gosplan, "fetch_purchase", return_value={"max_price": "210375.00"}), \
             mock.patch("tender_selection.stats.time.sleep"):
            collect_price_stats(catalog_requests=4, nmck_requests=6, categories=["17.23"])
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter([self.CONTRACT])), \
             mock.patch.object(gosplan, "fetch_purchase") as fp, \
             mock.patch("tender_selection.stats.time.sleep"):
            run2 = collect_price_stats(catalog_requests=4, nmck_requests=6, categories=["17.23"])
        fp.assert_not_called()
        self.assertEqual(run2.created_count, 0)

    def test_nmck_fill_respects_request_budget(self):
        from tender_selection.stats import collect_price_stats
        many = [dict(self.CONTRACT, purchase_number=f"n{i}", reg_num=f"R{i}") for i in range(8)]
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter(many)), \
             mock.patch.object(gosplan, "fetch_purchase", return_value={"max_price": "1000.00"}) as fp, \
             mock.patch("tender_selection.stats.time.sleep"):
            collect_price_stats(catalog_requests=4, nmck_requests=3, categories=["17.23"])
        self.assertEqual(fp.call_count, 3)

    def test_api_error_recorded_not_raised(self):
        from tender_selection.stats import collect_price_stats
        with mock.patch.object(gosplan, "iter_contracts", side_effect=gosplan.GosplanError("HTTP 429")), \
             mock.patch("tender_selection.stats.time.sleep"):
            run = collect_price_stats(catalog_requests=4, nmck_requests=6, categories=["17.23"])
        self.assertFalse(run.ok)
        self.assertIn("429", run.error)

    def test_multi_contract_purchase_marked_shared_and_skipped(self):
        from tender_selection.models import ContractStat
        from tender_selection.stats import collect_price_stats
        two = [
            dict(self.CONTRACT, purchase_number="P1", reg_num="RA", price="100000.00"),
            dict(self.CONTRACT, purchase_number="P1", reg_num="RB", price="300000.00"),
        ]
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter(two)), \
             mock.patch.object(gosplan, "fetch_purchase", return_value={"max_price": "1000000.00"}) as fp, \
             mock.patch("tender_selection.stats.time.sleep"):
            collect_price_stats(catalog_requests=4, nmck_requests=6, categories=["17.23"])
        rows = list(ContractStat.objects.filter(purchase_number="P1"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.shared_purchase for r in rows))
        self.assertTrue(all(r.discount_pct is None for r in rows))
        fp.assert_not_called()

    def test_implausible_discount_dropped_but_nmck_kept(self):
        from tender_selection.models import ContractStat
        from tender_selection.stats import collect_price_stats
        with mock.patch.object(gosplan, "iter_contracts", return_value=iter([self.CONTRACT])), \
             mock.patch.object(gosplan, "fetch_purchase", return_value={"max_price": "5000000.00"}), \
             mock.patch("tender_selection.stats.time.sleep"):
            collect_price_stats(catalog_requests=4, nmck_requests=6, categories=["17.23"])
        stat = ContractStat.objects.get(purchase_number="0111")
        self.assertEqual(str(stat.nmck), "5000000.00")
        self.assertIsNone(stat.discount_pct)  # 96.6% — артефакт, отброшено
        self.assertTrue(stat.nmck_checked)


class PriceStatsCardTests(TestCase):
    def _seed(self, discounts, *, cat="32.99", nmck="500000", region=77):
        import datetime
        from decimal import Decimal as D
        from tender_selection.models import ContractStat
        for i, d in enumerate(discounts):
            ContractStat.objects.create(
                law="fz44", purchase_number=f"p{i}", contract_reg_num=f"r{i}",
                category=cat, region=region, subject=f"Поставка партии {i}",
                nmck=D(nmck), final_price=D(nmck) * (100 - d) // 100,
                discount_pct=D(d), nmck_checked=True, contract_date=datetime.date(2026, 9, 1),
            )

    def _tender(self, **kw):
        from decimal import Decimal as D
        defaults = dict(purchase_number="X", law="fz44", object_info="x", title="T",
                        okpd2=["32.99.11"], max_price=D("500000"), region=77,
                        last_pulled_at=timezone.now())
        defaults.update(kw)
        return FoundTender.objects.create(**defaults)

    def test_none_when_too_few_samples(self):
        from tender_selection.stats import price_stats_for
        self._seed((20, 30))
        self.assertIsNone(price_stats_for(self._tender()))

    def test_none_for_fz223(self):
        from tender_selection.stats import price_stats_for
        self._seed((10, 20, 30, 40, 50, 55))
        self.assertIsNone(price_stats_for(self._tender(law="fz223")))

    def test_aggregates_median_range_examples(self):
        from tender_selection.stats import price_stats_for
        self._seed((10, 20, 30, 40, 50, 55))
        s = price_stats_for(self._tender())
        self.assertEqual(s["count"], 6)
        self.assertEqual(s["median"], 35)               # median(10,20,30,40,50,55)
        self.assertEqual(s["suggested_reduction"], 35)
        self.assertEqual(s["same_region"], 6)
        self.assertEqual(s["categories"], ["32.99"])
        self.assertEqual(len(s["examples"]), 6)

    def test_suggested_reduction_clamped(self):
        from tender_selection.stats import price_stats_for
        self._seed((70, 72, 75, 78, 80, 80))            # median 76.5 -> clamp to 60
        self.assertEqual(price_stats_for(self._tender())["suggested_reduction"], 60)

    def test_detail_view_shows_section(self):
        from django.contrib.auth import get_user_model
        for name in ("fetch_clarifications", "fetch_complaints"):
            p = mock.patch.object(gosplan, name, return_value=[]); p.start(); self.addCleanup(p.stop)
        self._seed((15, 25, 35, 45, 50, 55))
        tender = self._tender()
        self.client.force_login(get_user_model().objects.create_superuser("a", "a@e.ru", "p"))
        with mock.patch.object(gosplan, "fetch_notification", side_effect=gosplan.GosplanError("x")):
            resp = self.client.get(reverse("tender_selection:detail", args=[tender.pk]))
        self.assertContains(resp, "Снижение цены на похожих закупках")
        self.assertContains(resp, "подставим снижение")


class PriceStatsRelevanceTests(TestCase):
    def _row(self, i, discount, **kw):
        import datetime
        from decimal import Decimal as D
        from tender_selection.models import ContractStat
        data = dict(
            law="fz44", purchase_number=f"p{i}", contract_reg_num=f"r{i}", category="32.99",
            region=1, subject="", nmck=D("500000"), final_price=D("400000"),
            discount_pct=D(discount), nmck_checked=True, contract_date=datetime.date(2026, 9, 1),
        )
        data.update(kw)
        return ContractStat.objects.create(**data)

    def _tender(self, **kw):
        from decimal import Decimal as D
        base = dict(
            purchase_number="T", law="fz44", object_info="x", title="Поставка ежедневников с логотипом",
            okpd2=["32.99.12.110"], max_price=D("500000"), region=77, customer_inn="7700000000",
            last_pulled_at=timezone.now(),
        )
        base.update(kw)
        return FoundTender.objects.create(**base)

    def test_exact_code_plus_keyword_gives_strong_match(self):
        from tender_selection.stats import price_stats_for
        for i in range(5):
            self._row(i, 30 + i, okpd2=["32.99.12.110"], subject="Поставка ежедневников")
        for i in range(5, 15):  # шум: та же категория, другой товар
            self._row(i, 4, subject="Поставка сувенирной продукции")
        s = price_stats_for(self._tender())
        self.assertEqual(s["match_level"], "strong")
        self.assertEqual(s["count"], 5)
        self.assertEqual(s["median"], 32)

    def test_falls_back_to_category_when_no_strong(self):
        from tender_selection.stats import price_stats_for
        for i in range(8):
            self._row(i, 20 + i, subject="Поставка сувенирной продукции")
        s = price_stats_for(self._tender())
        self.assertEqual(s["match_level"], "category")
        self.assertEqual(s["count"], 8)

    def test_customer_history_line(self):
        from tender_selection.stats import price_stats_for
        for i in range(6):
            self._row(i, 40, subject="Поставка канцтоваров")
        self._row(20, 20, customer_inn="7700000000")
        self._row(21, 50, customer_inn="7700000000")
        self._row(22, 35, customer_inn="7700000000")
        s = price_stats_for(self._tender())
        self.assertIsNotNone(s["customer_stats"])
        self.assertEqual(s["customer_stats"]["count"], 3)
        self.assertEqual(s["customer_stats"]["median"], 35)

    def test_keywords_ignore_boilerplate(self):
        from tender_selection.stats import _tender_keywords
        t = self._tender(title="Запрос котировок в электронной форме на поставку товаров для нужд учреждения")
        self.assertEqual(_tender_keywords(t), set())


class PushWithStatsTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.admin = get_user_model().objects.create_superuser("a", "a@e.ru", "p")
        for name in ("fetch_clarifications", "fetch_complaints"):
            p = mock.patch.object(gosplan, name, return_value=[]); p.start(); self.addCleanup(p.stop)

    def test_push_uses_suggested_reduction_and_stashes_summary(self):
        import datetime
        from decimal import Decimal as D
        from tender_selection.models import ContractStat
        from tender_selection.services import push_to_estimate
        for i, d in enumerate((20, 30, 30, 40, 45, 50)):  # median 35
            ContractStat.objects.create(
                law="fz44", purchase_number=f"c{i}", contract_reg_num=f"cr{i}", category="32.99",
                nmck=D("500000"), final_price=D("300000"), discount_pct=D(d),
                nmck_checked=True, contract_date=datetime.date(2026, 9, 1),
            )
        tender = FoundTender.objects.create(
            purchase_number="Z", law="fz44", object_info="x", title="T", max_price=D("500000"),
            last_pulled_at=timezone.now(), notification_raw=NOTIFICATION_FIXTURE,
        )
        est_id = push_to_estimate(tender, self.admin)
        from tenders.models import TenderEstimate
        est = TenderEstimate.objects.get(pk=est_id)
        self.assertEqual(str(est.reduction_percent), "35.00")
        self.assertEqual(est.summary_snapshot["price_stats"]["median"], 35)

    def test_push_without_stats_keeps_default_30(self):
        from decimal import Decimal as D
        from tender_selection.services import push_to_estimate
        tender = FoundTender.objects.create(
            purchase_number="Z", law="fz44", object_info="x", title="T", max_price=D("500000"),
            last_pulled_at=timezone.now(), notification_raw=NOTIFICATION_FIXTURE,
        )
        est_id = push_to_estimate(tender, self.admin)
        from tenders.models import TenderEstimate
        est = TenderEstimate.objects.get(pk=est_id)
        self.assertEqual(str(est.reduction_percent), "30.00")
        self.assertNotIn("price_stats", est.summary_snapshot)


class PullCommandTests(TestCase):
    def test_non_loop_runs_one_pull(self):
        from django.core.management import call_command
        with mock.patch("tender_selection.management.commands.pull_tenders.run_pull") as rp:
            rp.return_value = mock.Mock(id=1, requests_made=1, records_received=0, created_count=0,
                                       updated_count=0, duration_seconds=1.0, ok=True, error="")
            call_command("pull_tenders", "--targeted")
        rp.assert_called_once()
        self.assertIsNone(rp.call_args.kwargs["classifiers"])


class IterPurchasesThrottleTests(TestCase):
    def test_stops_at_max_requests_and_sleeps_between(self):
        pages = [[{"purchase_number": str(i)} for i in range(100)] for _ in range(5)]
        calls = []

        def fake_fetch(params, law="fz44"):
            calls.append(params["skip"])
            return pages[len(calls) - 1]

        with mock.patch.object(gosplan, "fetch_page", side_effect=fake_fetch), \
             mock.patch.object(gosplan.time, "sleep") as sleep:
            got = list(gosplan.iter_purchases(params={}, max_requests=2))

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(got), 200)
        self.assertEqual(sleep.call_count, 1)  # спит только между 1-м и 2-м запросом


class FilteringTests(TestCase):
    def test_plus_words_any_match_passes(self):
        inc = parse_terms("сувенир, поло, кружк")
        self.assertEqual(match_title("Поставка сувенирной продукции", inc, [])[0], True)
        self.assertEqual(match_title("Поставка бетона", inc, [])[0], False)

    def test_empty_plus_words_passes_everything(self):
        self.assertEqual(match_title("что угодно", [], [])[0], True)

    def test_minus_word_hides(self):
        inc = parse_terms("сувенир")
        exc = parse_terms("медал")  # подстрока, ловит «медали» и «медаль»
        passes, _ = match_title("Сувенирные медали", inc, exc)
        self.assertFalse(passes)

    def test_plus_inside_entry_needs_all_parts(self):
        inc = parse_terms("живых+цветов")
        self.assertFalse(match_title("Поставка живых кроликов", inc, [])[0])
        self.assertTrue(match_title("Букеты из живых цветов", inc, [])[0])

    def test_case_and_yo_insensitive(self):
        inc = parse_terms("ЁЛка")
        self.assertTrue(match_title("Поставка елочных игрушек", parse_terms("елочн"), [])[0])
        self.assertTrue(match_title("Новогодняя ЁЛКА", inc, [])[0])


class FormattingTests(TestCase):
    def test_rub_grouped_digits(self):
        from .templatetags.tender_selection_extras import rub
        nb = "\N{NO-BREAK SPACE}"
        self.assertEqual(rub(23966770), f"23{nb}966{nb}770{nb}₽")
        self.assertEqual(rub(1300000), f"1{nb}300{nb}000{nb}₽")
        self.assertEqual(rub(90000), f"90{nb}000{nb}₽")
        self.assertEqual(rub(None), "—")

    def test_region_name(self):
        from .regions import region_name
        self.assertEqual(region_name(78), "Санкт-Петербург")
        self.assertEqual(region_name(38), "Иркутская обл.")
        self.assertEqual(region_name(999), "регион 999")

    def test_parse_dt_treats_naive_as_utc(self):
        from .services import _parse_dt
        dt = _parse_dt("2026-09-15T06:00:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertEqual(dt.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Moscow")).hour, 9)


class SettingsViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@e.ru", "pw")
        self.client.force_login(self.admin)

    def test_save_settings_persists_and_filters_list(self):
        for i, title in enumerate(["Поставка сувенирной продукции", "Поставка щебня"]):
            FoundTender.objects.create(
                purchase_number=str(i), object_info=title, title=title,
                max_price=500000, last_pulled_at=timezone.now(),
            )
        self.client.post(reverse("tender_selection:settings"), {
            "include_words": "сувенир", "exclude_words": "", "min_price": "300000", "window_days": "7",
        })
        s = FilterSettings.load()
        self.assertEqual(s.include_words, "сувенир")
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertContains(resp, "сувенирной")
        self.assertNotContains(resp, "щебня")
        self.assertContains(resp, "скрыто 1")

    def test_min_price_filters_list(self):
        FoundTender.objects.create(
            purchase_number="1", object_info="x", title="Дорогая кружка", max_price=100000, last_pulled_at=timezone.now()
        )
        FilterSettings.objects.update_or_create(pk=1, defaults={"min_price": 300000})
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertNotContains(resp, "Дорогая кружка")
        self.assertContains(resp, "Ничего не подходит под фильтр")

    def test_sort_by_deadline(self):
        import datetime
        near = timezone.now() + datetime.timedelta(days=2)
        far = timezone.now() + datetime.timedelta(days=20)
        FoundTender.objects.create(purchase_number="near", object_info="A", title="A", collecting_finished_at=near, last_pulled_at=timezone.now())
        FoundTender.objects.create(purchase_number="far", object_info="B", title="B", collecting_finished_at=far, last_pulled_at=timezone.now())
        resp = self.client.get(reverse("tender_selection:list") + "?sort=deadline")
        body = resp.content.decode()
        self.assertLess(body.index("№ near"), body.index("№ far"))


NOTIFICATION_FIXTURE = {
    "doc_type": "epNotificationEF2020",
    "source": {
        "commonInfo": {
            "href": "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=1",
            "purchaseObjectInfo": "Поставка сувенирной продукции",
            "placingWay": {"name": "Электронный аукцион"},
            "ETP": {"name": "Сбербанк-АСТ", "url": "http://sberbank-ast.ru"},
        },
        "purchaseResponsibleInfo": {
            "responsibleOrgInfo": {
                "fullName": "ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ \"ПРИМЕР\"",
                "shortName": "ГБУ ПРИМЕР",
                "INN": "7700000000",
                "KPP": "770000000",
                "factAddress": "г Москва, ул Тестовая, 1",
            },
            "responsibleInfo": {
                "contactPersonInfo": {"lastName": "Иванов", "firstName": "Иван"},
                "contactEMail": "z@example.ru",
                "contactPhone": "7-495-0000000",
            },
        },
        "attachmentsInfo": {
            "attachmentInfo": [
                {
                    "fileName": "Описание объекта закупки.docx",
                    "fileSize": "8379349",
                    "docKindInfo": {"name": "Описание объекта закупки"},
                    "url": "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=A",
                },
                {"fileName": "злая ссылка", "url": "https://evil.example/x"},
            ]
        },
        "notificationInfo": {
            "procedureInfo": {
                "collectingInfo": {"startDT": "2026-09-07T18:00:00+03:00", "endDT": "2026-09-15T09:00:00+03:00"},
                "biddingDate": "2026-09-16+03:00",
            },
            "contractConditionsInfo": {"maxPriceInfo": {"maxPrice": "1105700.00", "currency": {"name": "РОССИЙСКИЙ РУБЛЬ"}}},
            "customerRequirementsInfo": {
                "customerRequirementInfo": {
                    "applicationGuarantee": {"amount": "11057.00", "part": "1.0"},
                    "contractGuarantee": {"part": "5.0"},
                    "contractConditionsInfo": {
                        "deliveryPlacesInfo": {"byGARInfo": {"GARInfo": {"GARAddress": "г. Москва, ул. Пика, д. 4"}}},
                        "bankSupportContractRequiredInfo": {"treasurySupportContractInfo": {"treasurySupportContractRequired": "true"}},
                    },
                }
            },
            "purchaseObjectsInfo": {
                "notDrugPurchaseObjectsInfo": {
                    "purchaseObject": {
                        "name": "Баннер",
                        "OKPD2": {
                            "OKPDCode": "32.99.53.190",
                            "OKPDName": "Изделия демонстрационные прочие",
                            "characteristics": {
                                "characteristicsUsingTextForm": {
                                    "name": "Описание",
                                    "values": {"value": {"qualityDescription": "Размер 200х200, баннерная ткань"}},
                                }
                            },
                        },
                        "OKEI": {"nationalCode": "шт"},
                        "price": "3700.00",
                        "quantity": {"value": "1.00000000000"},
                        "sum": "3700.00",
                    }
                }
            },
        },
    },
}


class NotificationParseTests(TestCase):
    def setUp(self):
        from .notification import parse_notification
        self.card = parse_notification(NOTIFICATION_FIXTURE)

    def test_customer_and_contacts(self):
        self.assertIn("ПРИМЕР", self.card["customer"]["name"])
        self.assertEqual(self.card["customer"]["inn"], "7700000000")
        self.assertEqual(self.card["customer"]["contact_person"], "Иванов Иван")

    def test_dates_and_money(self):
        self.assertEqual(self.card["dates"]["collect_end"].hour, 9)
        self.assertEqual(self.card["money"]["max_price"], "1105700.00")
        self.assertEqual(self.card["money"]["app_guarantee_part"], "1.0")
        self.assertTrue(self.card["money"]["treasury_support"])

    def test_single_item_becomes_list_with_characteristics(self):
        self.assertEqual(len(self.card["items"]), 1)
        item = self.card["items"][0]
        self.assertEqual(item["name"], "Баннер")
        self.assertEqual(item["code"], "32.99.53.190")
        self.assertEqual(item["characteristics"][0]["value"], "Размер 200х200, баннерная ткань")

    def test_only_zakupki_documents_kept(self):
        self.assertEqual(len(self.card["documents"]), 1)
        self.assertEqual(self.card["documents"][0]["size_kb"], 8183)
        self.assertTrue(self.card["documents"][0]["url"].startswith("https://zakupki.gov.ru/"))

    def test_delivery_address(self):
        self.assertIn("Пика", self.card["delivery_address"])


CLARIFICATION_FIXTURE = [
    {
        "doc_type": "epClarificationOfDocumentationEZ44",
        "published_at": "2026-09-05T10:00:00",
        "source": {
            "commonInfo": {
                "docNumber": "РД1",
                "href": "https://zakupki.gov.ru/epz/order/notice/ea44/view/clarifications.html?regNumber=1",
            },
            "clarificationInfo": {
                "questionText": "Просим уточнить требуемый размер флага и материал полотна для позиции 1.",
                "answerText": "Размер флага 90x135 см, материал — флажная сетка, плотность 115 г/кв.м.",
            },
            "extPrintFormInfo": {
                "url": "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=CLR",
            },
        },
    },
    {"doc_type": "epExplanation", "published_at": "2026-09-04T09:00:00", "source": {}},
]

COMPLAINT_FIXTURE = [
    {
        "reg_number": "202600126646001665",
        "published_at": "2026-09-07T19:34:32",
        "updated_at": "2026-09-07T19:34:32",
        "purchase_number": "1",
        "object": "purchase",
        "region": 15,
        "docs": [{"doc_type": "complaint", "published_at": "2026-09-07T19:34:32"}],
    },
]


class ClarificationComplaintParseTests(TestCase):
    def test_clarification_extracts_question_and_answer(self):
        from .notification import parse_clarifications
        rows = parse_clarifications(CLARIFICATION_FIXTURE)
        self.assertEqual(len(rows), 2)
        first = rows[0]  # новее — от 05.09
        self.assertEqual(first["label"], "Разъяснение документации")
        self.assertIn("размер флага", first["question"])
        self.assertIn("90x135", first["answer"])
        self.assertTrue(first["print_url"].startswith("https://zakupki.gov.ru/"))
        self.assertTrue(first["href"].startswith("https://zakupki.gov.ru/"))

    def test_clarification_minimal_source_still_a_row(self):
        from .notification import parse_clarifications
        bare = parse_clarifications(CLARIFICATION_FIXTURE)[1]
        self.assertEqual(bare["label"], "Разъяснение")
        self.assertEqual(bare["question"], "")
        self.assertEqual(bare["answer"], "")
        self.assertIsNotNone(bare["published_at"])

    def test_complaints_metadata(self):
        from .notification import parse_complaints
        rows = parse_complaints(COMPLAINT_FIXTURE)
        self.assertEqual(rows[0]["kinds"], ["Жалоба"])
        self.assertEqual(rows[0]["object"], "закупка")
        self.assertEqual(rows[0]["reg_number"], "202600126646001665")


class ExtrasForTests(TestCase):
    def setUp(self):
        self.tender = FoundTender.objects.create(
            purchase_number="0345", law="fz44", object_info="x", title="T", last_pulled_at=timezone.now(),
        )

    def test_first_call_fetches_then_cache_serves_repeat(self):
        from .services import extras_for
        with mock.patch.object(gosplan, "fetch_clarifications", return_value=CLARIFICATION_FIXTURE) as fc, \
             mock.patch.object(gosplan, "fetch_complaints", return_value=COMPLAINT_FIXTURE):
            clar, comp = extras_for(self.tender)
        self.assertEqual(len(clar), 2)
        self.assertEqual(len(comp), 1)
        fc.assert_called_once()
        self.tender.refresh_from_db()
        self.assertIsNotNone(self.tender.extras_checked_at)
        with mock.patch.object(gosplan, "fetch_clarifications") as fc2, \
             mock.patch.object(gosplan, "fetch_complaints") as fk2:
            extras_for(self.tender)
        fc2.assert_not_called()
        fk2.assert_not_called()

    def test_force_refetches(self):
        from .services import extras_for
        with mock.patch.object(gosplan, "fetch_clarifications", return_value=[]), \
             mock.patch.object(gosplan, "fetch_complaints", return_value=[]):
            extras_for(self.tender)
        with mock.patch.object(gosplan, "fetch_clarifications", return_value=CLARIFICATION_FIXTURE) as fc, \
             mock.patch.object(gosplan, "fetch_complaints", return_value=[]):
            clar, _ = extras_for(self.tender, force=True)
        fc.assert_called_once()
        self.assertEqual(len(clar), 2)

    def test_fz223_skips_api(self):
        from .services import extras_for
        t = FoundTender.objects.create(purchase_number="9", law="fz223", object_info="x", last_pulled_at=timezone.now())
        with mock.patch.object(gosplan, "fetch_clarifications") as fc:
            self.assertEqual(extras_for(t), ([], []))
        fc.assert_not_called()

    def test_api_error_neither_raises_nor_caches(self):
        from .services import extras_for
        with mock.patch.object(gosplan, "fetch_clarifications", side_effect=gosplan.GosplanError("429")), \
             mock.patch.object(gosplan, "fetch_complaints", side_effect=gosplan.GosplanError("429")):
            self.assertEqual(extras_for(self.tender), ([], []))
        self.tender.refresh_from_db()
        self.assertIsNone(self.tender.extras_checked_at)


class ClarificationComplaintViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@e.ru", "pw")
        self.client.force_login(self.admin)
        self.tender = FoundTender.objects.create(
            purchase_number="1", law="fz44", object_info="x", title="Флаги с логотипом",
            last_pulled_at=timezone.now(), notification_raw=NOTIFICATION_FIXTURE,
        )

    def test_detail_renders_both_sections(self):
        with mock.patch.object(gosplan, "fetch_clarifications", return_value=CLARIFICATION_FIXTURE), \
             mock.patch.object(gosplan, "fetch_complaints", return_value=COMPLAINT_FIXTURE):
            resp = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertContains(resp, "Разъяснения")
        self.assertContains(resp, "90x135")
        self.assertContains(resp, "Жалобы в ФАС")
        self.assertContains(resp, "202600126646001665")

    def test_complaint_flag_shows_in_list_after_check(self):
        with mock.patch.object(gosplan, "fetch_clarifications", return_value=[]), \
             mock.patch.object(gosplan, "fetch_complaints", return_value=COMPLAINT_FIXTURE):
            self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertContains(resp, "ts-flag")


class DetailViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@e.ru", "pw")
        self.client.force_login(self.admin)
        for name in ("fetch_clarifications", "fetch_complaints"):
            patcher = mock.patch.object(gosplan, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tender = FoundTender.objects.create(
            purchase_number="0345500000626000081", object_info="x", title="Сувенирка",
            customer_inn="7700000000", last_pulled_at=timezone.now(),
        )

    def test_fetches_and_renders_card(self):
        with mock.patch.object(gosplan, "fetch_notification", return_value=NOTIFICATION_FIXTURE) as fetch:
            resp = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Баннер")
        self.assertContains(resp, "Описание объекта закупки.docx")
        self.assertContains(resp, "ГБУ ПРИМЕР")
        fetch.assert_called_once()
        self.tender.refresh_from_db()
        self.assertTrue(self.tender.notification_raw)

    def test_second_open_uses_cache_no_request(self):
        with mock.patch.object(gosplan, "fetch_notification", return_value=NOTIFICATION_FIXTURE):
            self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        with mock.patch.object(gosplan, "fetch_notification") as fetch:
            resp = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        fetch.assert_not_called()
        self.assertEqual(resp.status_code, 200)

    def test_customer_name_backfilled_to_list(self):
        with mock.patch.object(gosplan, "fetch_notification", return_value=NOTIFICATION_FIXTURE):
            self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertTrue(Organization.objects.filter(inn="7700000000", name__icontains="ПРИМЕР").exists())

    def test_fetch_failure_renders_fallback(self):
        with mock.patch.object(gosplan, "fetch_notification", side_effect=gosplan.RateLimitError("429")):
            resp = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Не удалось загрузить извещение")


class AccessControlTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@e.ru", "pw")
        self.user = User.objects.create_user("bob", "b@e.ru", "pw")

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_regular_user_forbidden(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertEqual(resp.status_code, 403)

    def test_superuser_sees_the_list(self):
        FoundTender.objects.create(
            purchase_number="1", object_info="x", title="Кружки", last_pulled_at=timezone.now()
        )
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Кружки")

    def test_set_review_ajax(self):
        tender = FoundTender.objects.create(
            purchase_number="1", object_info="x", last_pulled_at=timezone.now()
        )
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("tender_selection:review", args=[tender.pk]),
            {"review": "interesting"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.json()["review"], "interesting")
        tender.refresh_from_db()
        self.assertEqual(tender.review, FoundTender.INTERESTING)

    def test_set_review_rejects_unknown_value(self):
        tender = FoundTender.objects.create(
            purchase_number="1", object_info="x", last_pulled_at=timezone.now()
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("tender_selection:review", args=[tender.pk]), {"review": "bogus"})
        tender.refresh_from_db()
        self.assertEqual(tender.review, FoundTender.UNREVIEWED)

    def test_review_dot_class_in_list(self):
        FoundTender.objects.create(
            purchase_number="1", object_info="x", title="Кружка", review=FoundTender.NOT_INTERESTING,
            last_pulled_at=timezone.now(),
        )
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("tender_selection:list"))
        self.assertContains(resp, "saved-estimate__status-form is-not_interesting")

    def test_dismiss_hides_row(self):
        tender = FoundTender.objects.create(
            purchase_number="1", object_info="x", last_pulled_at=timezone.now()
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("tender_selection:dismiss", args=[tender.pk]))
        tender.refresh_from_db()
        self.assertEqual(tender.status, FoundTender.DISMISSED)
