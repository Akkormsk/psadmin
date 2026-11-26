from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from tender_selection.models import Tender
from .models import Order, Task, TenderCommercialItem, TenderComputeJob, TenderSourceItem


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
            with self.assertRaises(ValidationError):
                item.source_items.add(self.foreign_source)
        for item in (order_item, task_item):
            with self.assertRaises(ValidationError):
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
