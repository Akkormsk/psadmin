from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from tender_selection.models import Tender
from .models import CalculationComponent, Order, Task, TenderCommercialItem, TenderComputeJob, TenderSourceItem


class CommercialItemOwnershipTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="owners-1", title="Tender")
        self.other = Tender.objects.create(purchase_number="owners-2", title="Other")
        self.order = Order.objects.create(name="Order")
        self.task = Task.objects.create(name="Task")
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key="s", original_text="Item")
        self.foreign_source = TenderSourceItem.objects.create(tender=self.other, source_key="s", original_text="Other")
        self.job = TenderComputeJob.objects.create(tender=self.tender)
        self.foreign_job = TenderComputeJob.objects.create(tender=self.other)

    def item(self, **kwargs):
        return TenderCommercialItem.objects.create(source_key="same", display_name="Item", **kwargs)

    def test_three_owners_are_isolated(self):
        tender_item = self.item(tender=self.tender, job=self.job)
        tender_item.source_items.add(self.source)
        order_item = self.item(order=self.order)
        task_item = self.item(task=self.task)
        self.assertEqual(list(self.tender.v2_commercial_items.all()), [tender_item])
        self.assertEqual(list(self.order.commercial_items.all()), [order_item])
        self.assertEqual(list(self.task.commercial_items.all()), [task_item])
        for owner in ({"tender": self.tender}, {"order": self.order}, {"task": self.task}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.item(**owner)

    def test_database_requires_exactly_one_owner(self):
        for owners in ({}, {"tender": self.tender, "order": self.order},
                       {"order": self.order, "task": self.task},
                       {"tender": self.tender, "task": self.task},
                       {"tender": self.tender, "order": self.order, "task": self.task}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.item(**owners)

    def test_source_links_are_checked_in_both_directions(self):
        tender_item = self.item(tender=self.tender)
        order_item = self.item(order=self.order)
        task_item = self.item(task=self.task)
        for item in (tender_item, order_item, task_item):
            with self.assertRaises(ValidationError), transaction.atomic():
                item.source_items.add(self.foreign_source)
        for item in (order_item, task_item):
            with self.assertRaises(ValidationError), transaction.atomic():
                self.source.commercial_items.add(item)
        self.source.commercial_items.add(tender_item)
        self.assertEqual(list(tender_item.source_items.all()), [self.source])

    def test_job_links_are_checked(self):
        for owner in ({"order": self.order}, {"task": self.task}, {"tender": self.tender}):
            with self.assertRaises(ValidationError):
                self.item(job=self.foreign_job, **owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TenderCommercialItem.objects.bulk_create([
                TenderCommercialItem(order=self.order, job=self.job, source_key="bulk", display_name="Item")
            ])

    def test_existing_source_prevents_owner_reassignment(self):
        item = self.item(tender=self.tender)
        item.source_items.add(self.source)
        item.tender = None
        item.order = self.order
        with self.assertRaises(ValidationError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.tender_id, self.tender.pk)
        self.assertIsNone(item.order_id)


class CalculationComponentWorkboardTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="components-1", title="Tender")
        self.other_tender = Tender.objects.create(purchase_number="components-2", title="Other")
        self.order = Order.objects.create(name="Order")
        self.task = Task.objects.create(name="Task")
        self.source = TenderSourceItem.objects.create(
            tender=self.tender, source_key="source", original_text="Product",
        )
        self.other_source = TenderSourceItem.objects.create(
            tender=self.other_tender, source_key="other", original_text="Other product",
        )
        self.sibling_source = TenderSourceItem.objects.create(
            tender=self.tender, source_key="sibling", original_text="Sibling product",
        )

    def item(self, *, owner, key, quantity=None):
        return TenderCommercialItem.objects.create(
            source_key=key, display_name=key, quantity=quantity, **owner,
        )

    def test_task_component_accepts_description_without_quantity(self):
        item = self.item(owner={"task": self.task}, key="task-step")

        component = CalculationComponent.objects.create(
            commercial_item=item,
            name="Prepare requirements",
            description="Collect examples and agree the result.",
            quantity_per_parent=None,
        )

        self.assertEqual(component.description, "Collect examples and agree the result.")
        self.assertIsNone(component.effective_quantity)

    def test_commercial_components_keep_quantity_default(self):
        tender_item = self.item(owner={"tender": self.tender}, key="tender-product", quantity=12)
        order_item = self.item(owner={"order": self.order}, key="order-product", quantity=5)

        component = CalculationComponent.objects.create(commercial_item=tender_item, name="Product")

        self.assertEqual(component.quantity_per_parent, 1)
        self.assertEqual(component.effective_quantity, 12)
        for item in (tender_item, order_item):
            with self.assertRaises(ValidationError):
                CalculationComponent.objects.create(
                    commercial_item=item, name="Missing quantity", quantity_per_parent=None,
                )

    def test_parent_and_source_stay_with_the_working_item(self):
        item = self.item(owner={"tender": self.tender}, key="main", quantity=2)
        item.source_items.add(self.source)
        sibling_item = self.item(owner={"tender": self.tender}, key="sibling", quantity=4)
        sibling_item.source_items.add(self.sibling_source)
        other_item = self.item(owner={"tender": self.other_tender}, key="other", quantity=3)
        other_item.source_items.add(self.other_source)
        parent = CalculationComponent.objects.create(
            commercial_item=item, source_item=self.source, name="Parent",
        )
        foreign_parent = CalculationComponent.objects.create(
            commercial_item=other_item, source_item=self.other_source, name="Foreign parent",
        )

        child = CalculationComponent.objects.create(
            commercial_item=item, source_item=self.source, parent=parent, name="Child",
        )
        self.assertEqual(child.parent_id, parent.pk)
        for values in (
            {"parent": foreign_parent},
            {"source_item": self.other_source},
            {"source_item": self.sibling_source},
        ):
            with self.assertRaises(ValidationError):
                CalculationComponent.objects.create(commercial_item=item, name="Invalid", **values)

        task_item = self.item(owner={"task": self.task}, key="task-source")
        with self.assertRaises(ValidationError):
            CalculationComponent.objects.create(
                commercial_item=task_item, source_item=self.source, name="Task source",
            )


class WorkboardComponentMigrationTests(TransactionTestCase):
    def test_existing_component_identity_and_links_survive_upgrade(self):
        previous = [("tenders", "0062_workboard_ownership")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(previous)
        try:
            old_apps = executor.loader.project_state(previous).apps
            task = old_apps.get_model("tenders", "Task").objects.create(name="Existing task")
            item = old_apps.get_model("tenders", "TenderCommercialItem").objects.create(
                task_id=task.pk, source_key="existing", display_name="Existing step",
            )
            component = old_apps.get_model("tenders", "CalculationComponent").objects.create(
                commercial_item_id=item.pk, name="Existing component", quantity_per_parent=2,
            )

            MigrationExecutor(connection).migrate(latest)
            saved = CalculationComponent.objects.get(pk=component.pk)
            self.assertEqual(saved.commercial_item_id, item.pk)
            self.assertEqual(saved.commercial_item.task_id, task.pk)
            self.assertEqual(saved.quantity_per_parent, 2)
            self.assertEqual(saved.description, "")

            executor = MigrationExecutor(connection)
            executor.migrate(previous)
            old_apps = executor.loader.project_state(previous).apps
            rolled_back = old_apps.get_model("tenders", "CalculationComponent").objects.get(pk=component.pk)
            self.assertEqual(rolled_back.commercial_item_id, item.pk)
            self.assertEqual(rolled_back.quantity_per_parent, 2)
        finally:
            MigrationExecutor(connection).migrate(latest)
