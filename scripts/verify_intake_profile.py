"""Bounded TEST shadow QA through existing classifier, both databases READ ONLY."""
import json
import os
import sys
from decimal import Decimal
os.environ['TENDER_AUTOPULL_ENABLED'] = '0'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
import django
django.setup()
from django.db import connection
from tender_selection.models import Tender, FilterSettings
from tender_selection.profile_triage import _answers, _state, _valid_answer

if connection.vendor != 'postgresql' or connection.settings_dict.get('HOST') != 'test-db':
    raise RuntimeError('Model verification is restricted to TEST')
budget = Decimal('3.12')
with connection.cursor() as cursor:
    cursor.execute('SET default_transaction_read_only = on')
selection = FilterSettings.load()
spent, results, calls = Decimal('0'), [], []
for row in json.load(sys.stdin):
    # Negative unsaved IDs cannot select unrelated TEST working positions.
    row['pk'] = -int(row['pk'])
    tender = Tender(**row)
    metrics = {}
    answers = _answers([tender], selection, max_cost_rub=budget-spent, diagnostics=metrics)
    spent += Decimal(str(metrics.get('cost_rub') or metrics.get('reserved_cost_rub') or 0))
    calls.append(metrics)
    states = {tender.pk: _state(tender, selection)}
    accepted = [_valid_answer(answer, states, diagnostics=metrics) for answer in answers]
    results.append({'number': tender.purchase_number, 'title': tender.title, 'answers': [answer for answer in accepted if answer]})
print('PROFILE_QA ' + json.dumps({'cost_rub': str(spent), 'calls': calls, 'results': results,
    'source_read_only': True, 'test_read_only': True, 'lessons_created': 0, 'manual_decisions_preserved': True}, ensure_ascii=False, default=str))
