from django.db import migrations, models


def backfill_archive_history(apps, schema_editor):
    Tender = apps.get_model("tender_selection", "Tender")
    ContractStat = apps.get_model("tender_selection", "ContractStat")
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    estimate_tender_ids = set(TenderEstimate.objects.values_list("tender_id", flat=True))
    stage_by_outcome = {
        "draft": "calculation", "not_participated": "calculation", "pending": "bidding",
        "published": "published", "lost": "lost", "won": "won",
    }
    for tender in Tender.objects.filter(status="dismissed", archived_from_stage="").iterator():
        if tender.pk in estimate_tender_ids:
            tender.archived_from_stage = stage_by_outcome.get(tender.outcome_status, "calculation")
        else:
            tender.archived_from_stage = "incoming" if tender.review == "unreviewed" else "evaluation"
        tender.save(update_fields=["archived_from_stage"])

    for tender in Tender.objects.filter(
        law="fz44", status="dismissed", contract_price__isnull=False,
        contract_reduction_percent__isnull=False,
    ).iterator():
        stat = ContractStat.objects.filter(law=tender.law, purchase_number=tender.purchase_number).first()
        if stat:
            if not stat.own_funnel:
                stat.own_funnel = True
                stat.save(update_fields=["own_funnel"])
            continue
        ContractStat.objects.create(
            law=tender.law,
            purchase_number=tender.purchase_number,
            contract_reg_num=tender.contract_reg_num or f"tender:{tender.purchase_number}",
            subject=tender.title or tender.object_info,
            okpd2=tender.okpd2,
            region=tender.region,
            nmck=tender.max_price,
            final_price=tender.contract_price,
            discount_pct=tender.contract_reduction_percent,
            own_funnel=True,
            is_ours=tender.outcome_status == "won",
            nmck_checked=True,
        )


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0032_contractstat_own_funnel")]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="archived_from_stage",
            field=models.CharField(blank=True, db_index=True, max_length=16, verbose_name="Этап при архивировании"),
        ),
        migrations.AddField(
            model_name="contractstat",
            name="forecast_included",
            field=models.BooleanField(db_index=True, default=True, verbose_name="Учитывать в прогнозе"),
        ),
        migrations.RunPython(backfill_archive_history, migrations.RunPython.noop),
    ]
