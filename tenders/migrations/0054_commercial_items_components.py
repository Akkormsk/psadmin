# Generated for Calculation Engine V2 commercial item/component model.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("tenders", "0053_ownerfeedbackevent_knowledgerecord_ownerinteraction_and_more")]

    operations = [
        migrations.CreateModel(
            name="TenderCommercialItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_key", models.CharField(max_length=160)),
                ("display_name", models.TextField()),
                ("quantity", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("unit", models.CharField(blank=True, max_length=64)),
                ("requirements", models.JSONField(blank=True, default=dict)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                ("structure", models.CharField(choices=[("simple", "Simple"), ("aggregate", "Aggregate"), ("composite", "Composite")], default="simple", max_length=24)),
                ("status", models.CharField(default="active", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="commercial_items", to="tenders.tendercomputejob")),
                ("tender", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="v2_commercial_items", to="tender_selection.tender")),
                ("source_items", models.ManyToManyField(related_name="commercial_items", to="tenders.tendersourceitem")),
            ],
            options={"ordering": ["pk"]},
        ),
        migrations.CreateModel(
            name="CalculationComponent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.TextField()),
                ("quantity_per_parent", models.DecimalField(decimal_places=4, default=1, max_digits=14)),
                ("unit", models.CharField(blank=True, max_length=64)),
                ("requirements", models.JSONField(blank=True, default=dict)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(default="active", max_length=32)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("commercial_item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="components", to="tenders.tendercommercialitem")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="children", to="tenders.calculationcomponent")),
                ("source_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="calculation_components", to="tenders.tendersourceitem")),
            ],
            options={"ordering": ["sort_order", "pk"]},
        ),
        migrations.CreateModel(
            name="ComponentRoutePlan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(choices=[("component", "Component"), ("shared", "Shared")], default="component", max_length=16)),
                ("status", models.CharField(default="planned", max_length=32)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("commercial_item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="route_plans", to="tenders.tendercommercialitem")),
                ("component", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="route_plans", to="tenders.calculationcomponent")),
            ],
        ),
        migrations.CreateModel(
            name="ComponentOperationStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                ("details", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(default="planned", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("process", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="v2_operation_steps", to="tenders.processdefinition")),
                ("route_plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="steps", to="tenders.componentrouteplan")),
            ],
            options={"ordering": ["position", "pk"]},
        ),
        migrations.AddField(model_name="tendercomputeline", name="commercial_item", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="compute_lines", to="tenders.tendercommercialitem")),
        migrations.AddField(model_name="tendercomputeline", name="component", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="compute_lines", to="tenders.calculationcomponent")),
        migrations.AddField(model_name="tendercomputeworkunit", name="component", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="work_units", to="tenders.calculationcomponent")),
        migrations.AddField(model_name="tendercomputeworkunit", name="operation_step", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="work_units", to="tenders.componentoperationstep")),
        migrations.AddConstraint(model_name="tendercommercialitem", constraint=models.UniqueConstraint(fields=("tender", "source_key"), name="unique_v2_commercial_item")),
        migrations.AddConstraint(model_name="componentoperationstep", constraint=models.UniqueConstraint(fields=("route_plan", "position"), name="unique_v2_route_step_position")),
    ]