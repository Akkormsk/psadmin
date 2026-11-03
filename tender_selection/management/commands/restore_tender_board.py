"""Explicit single-tender board restoration through the shared confirmed action."""
import json
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import RequestFactory
from django.urls import reverse
from lxml import html

from tender_selection.models import Tender
from tender_selection.services import restore_hidden_tender
from tender_selection.views import kanban


class Command(BaseCommand):
    help = 'Preview or explicitly restore one selected tender to the board without changing its deadline or financial calculations.'

    def add_arguments(self, parser):
        parser.add_argument('--tender-number', required=True)
        parser.add_argument('--keep-hours', type=int, default=168)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        number = options['tender_number']
        if len(number) != 19 or not number.isascii() or not number.isdigit():
            raise CommandError('Expected one purchase number')
        if not 1 <= options['keep_hours'] <= 168:
            raise CommandError('Expected 1 to 168 hours')
        tender = Tender.objects.select_for_update().get(purchase_number=number)
        before_deadline = tender.collecting_finished_at
        before_estimates = list(tender.estimates.values_list('pk', flat=True))
        report = {'tender_id': tender.pk, 'number': number, 'apply': options['apply'], 'deadline': before_deadline}
        if options['apply']:
            actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by('pk').first()
            if actor is None:
                raise CommandError('No active administrator')
            restore_hidden_tender(actor, tender.pk, options['keep_hours'])
            tender.refresh_from_db()
            request = RequestFactory().get('/tender-selection/', {'view': 'kanban'})
            request.user, request.session = actor, {}
            response = kanban(request)
            target = reverse('tender_selection:detail', args=[tender.pk])
            visible = any(urlsplit(value).path == target for value in html.fromstring(response.content).xpath('//a/@href'))
            if response.status_code != 200 or not visible:
                raise CommandError('Tender is not visible on actual board; restoration rolled back')
            if tender.collecting_finished_at != before_deadline or list(tender.estimates.values_list('pk', flat=True)) != before_estimates:
                raise CommandError('Unexpected deadline or financial calculation change; rolled back')
            report.update(status=tender.status, review=tender.review, visible_until=tender.kanban_visible_until, board_http_status=response.status_code, visible=visible, financial_calculations_unchanged=True)
        self.stdout.write(json.dumps(report, ensure_ascii=False, default=str))
