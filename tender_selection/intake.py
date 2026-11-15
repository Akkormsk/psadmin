"""One secondary decision per arrival; no primary filtering, HTTP or AI."""
import hashlib
import json
from collections import Counter
from django.db import transaction
from django.utils import timezone
from .filtering import match_title, parse_terms
from .models import FilterSettings, Tender, TenderHistory


def secondary_reason(tender, selection, *, preserve=False):
    if preserve:
        return '', []
    _, hits = match_title(tender.title or tender.object_info, parse_terms(selection.exclude_words), [])
    if tender.collecting_finished_at and tender.collecting_finished_at < timezone.now():
        return 'expired', hits
    return ('minus_words' if hits else ''), hits


def protected_existing(tender, history):
    return (tender.status != Tender.NEW or tender.review != Tender.UNREVIEWED
            or tender.opened_at is not None or history.decision_source == 'manual'
            or tender.kanban_visible_until is not None and tender.kanban_visible_until > timezone.now())


@transaction.atomic
def decide(tender_id, selection=None, *, existing=False, preserve_existing=False, primary_context=None):
    tender = Tender.objects.select_for_update().only(
        'pk', 'status', 'review', 'opened_at', 'kanban_visible_until', 'title', 'object_info',
        'collecting_finished_at', 'source',
    ).get(pk=tender_id)
    history = TenderHistory.objects.select_for_update().defer('legacy_data').get(tender_id=tender_id)
    if 'intake' in history.filter_snapshot:
        return tender.status != Tender.DISMISSED
    selection = selection or FilterSettings.load()
    preserve = existing and (preserve_existing or protected_existing(tender, history))
    reason, minus = secondary_reason(tender, selection, preserve=preserve)
    secondary = {'exclude_words': selection.exclude_words, 'expire_on_arrival': True}
    version = hashlib.sha256(json.dumps(secondary, sort_keys=True).encode()).hexdigest()
    plus = history.plus_hits if preserve else match_title(
        tender.title or tender.object_info, parse_terms(selection.include_words), [],
    )[1]
    now = timezone.now()
    snapshot = {**history.filter_snapshot,
        'primary': ({'preserved_existing': True, 'query_unrecorded': True} if preserve else
                    (primary_context or {'source': tender.source, 'query_unrecorded': True})),
        'secondary': {'rules_not_reapplied': True} if preserve else secondary,
        'intake': {'at': now.isoformat(), 'reason': reason, 'preserved_existing': bool(preserve),
                   'admitted': not bool(reason) and tender.status != Tender.DISMISSED}}
    if reason:
        tender.status = Tender.DISMISSED
        tender.archived_at = now
        tender.archived_from_stage = 'incoming'
        tender.save(update_fields=['status', 'archived_at', 'archived_from_stage'])
    history.filter_snapshot = snapshot
    history.rule_version = history.rule_version if preserve else version
    history.plus_hits = history.plus_hits if preserve else plus  # provenance only
    history.minus_hits = history.minus_hits if preserve else minus
    fields = ['filter_snapshot', 'rule_version', 'plus_hits', 'minus_hits']
    if not preserve:
        history.filtered_out = bool(minus)
        history.reason_code = reason
        history.reason_text = ('Минус-слова: ' + ', '.join(minus)) if reason == 'minus_words' else ('Истёк срок подачи' if reason else '')
        history.decision_source = 'auto'
        history.decision_at = now
        fields += ['filtered_out', 'reason_code', 'reason_text', 'decision_source', 'decision_at']
    history.save(update_fields=fields)
    return tender.status != Tender.DISMISSED


def initialize_existing(*, apply=False):
    selection = FilterSettings.load()
    counts = Counter(decided=0, archived=0, protected=0)
    pending = Tender.objects.exclude(history_record__filter_snapshot__has_key='intake').values_list('pk', flat=True)
    for tender_id in pending.iterator(chunk_size=200):
        counts['decided'] += 1
        counts['protected'] += 1
        if apply:
            decide(tender_id, selection, existing=True, preserve_existing=True)
    return dict(counts)


@transaction.atomic
def expire_incoming():
    """The only recurring exclusion: a known deadline has passed."""
    now = timezone.now()
    ids = list(Tender.objects.select_for_update().filter(status=Tender.NEW, review=Tender.UNREVIEWED,
        collecting_finished_at__lt=now).exclude(kanban_visible_until__gt=now).values_list('pk', flat=True))
    if not ids:
        return 0
    Tender.objects.filter(pk__in=ids, status=Tender.NEW, review=Tender.UNREVIEWED).update(
        status=Tender.DISMISSED, archived_at=now, archived_from_stage='incoming')
    TenderHistory.objects.filter(tender_id__in=ids, status=Tender.DISMISSED).update(
        reason_code='expired', reason_text='Истёк срок подачи', decision_source='auto', decision_at=now)
    return len(ids)
