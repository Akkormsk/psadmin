"""Shared writes for new/active tenders; legacy archive reads stay side-effect free."""
from django.db import DEFAULT_DB_ALIAS, transaction
from copy import deepcopy

from tender_selection.models import DocumentPreview, Tender, TenderDocument
from .models import TenderAnalysis


def prepare_tender_for_write(tender):
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = Tender.objects.using(using).select_for_update().get(pk=tender.pk)
        if current.status == Tender.DISMISSED:
            raise ValueError("Сначала верните тендер из архива в работу.")
        if current.data_version != Tender.DataVersion.CURRENT:
            current.data_version = Tender.DataVersion.CURRENT
            current.save(using=using, update_fields=['data_version'])
        from tender_selection.notification import parse_notification
        urls = {doc['url'] for doc in parse_notification(current.notification_raw or {}).get('documents', []) if doc.get('url')}
        links = list(current.document_versions.using(using).values('pk', 'source_key', 'preview_id', 'is_current'))
        linked = {link['source_key'] for link in links if link['is_current']}
        previews = document_preview_map(current, urls - linked, using=using)
        historical = {(link['source_key'], link['preview_id']): link['pk'] for link in links}
        create, reactivate = [], []
        for url, preview in previews.items():
            pk = historical.get((url, preview.pk))
            if pk is None:
                create.append(TenderDocument(tender=current, preview=preview, source_key=url))
            else:
                reactivate.append(pk)
        if reactivate:
            TenderDocument.objects.using(using).filter(pk__in=reactivate).update(is_current=True)
        if create:
            TenderDocument.objects.using(using).bulk_create(create)
        tender.data_version = current.data_version
        return current


def document_preview_map(tender, urls, *, using=None):
    using = using or (tender._state.db if tender else None) or DEFAULT_DB_ALIAS
    urls = set(urls)
    result = {}
    if tender is not None:
        links = TenderDocument.objects.using(using).filter(tender=tender, is_current=True, source_key__in=urls).select_related('preview').defer('assignments', 'preview__structure', 'preview__extraction')
        for link in links:
            if link.last_error:
                link.preview.error = link.last_error
            result[link.source_key] = link.preview
    fallback = DocumentPreview.objects.using(using).filter(url__in=urls - result.keys()).defer('structure', 'extraction').order_by('pk')
    if tender is not None and tender.data_version == Tender.DataVersion.LEGACY:
        fallback = fallback.filter(content_hash='')
    for preview in fallback:
        previous = result.get(preview.url)
        if previous is None or not preview.content_hash or previous.content_hash:
            result[preview.url] = preview
    return result


def publish_document_preview(tender, url, name, result, *, content_hash, parser_version):
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = prepare_tender_for_write(tender)
        previous = document_preview_map(current, [url], using=using).get(url)
        if previous is not None and previous.html and not result.get('html'):
            link, _ = TenderDocument.objects.using(using).get_or_create(tender=current, source_key=url, preview=previous)
            link.last_error = str(result.get('error') or 'Документ не удалось прочитать.')
            link.save(using=using, update_fields=['last_error', 'updated_at'])
            return previous
        preview, _ = DocumentPreview.objects.using(using).get_or_create(
            url=url, content_hash=content_hash, parser_version=parser_version,
            defaults={'filename': name, 'kind': result.get('kind', ''), 'html': result.get('html', ''),
                      'error': str(result.get('error') or '')[:DocumentPreview._meta.get_field('error').max_length]},
        )
        previous_ids = set(TenderDocument.objects.using(using).filter(
            tender=current, source_key=url, is_current=True).exclude(preview=preview).values_list('pk', flat=True))
        TenderDocument.objects.using(using).filter(tender=current, source_key=url, is_current=True).exclude(preview=preview).update(is_current=False)
        link, created = TenderDocument.objects.using(using).get_or_create(tender=current, source_key=url, preview=preview)
        if not created and (not link.is_current or link.last_error):
            link.is_current, link.last_error = True, ''
            link.save(using=using, update_fields=['is_current', 'last_error', 'updated_at'])
        if previous_ids and current.conditions:
            conditions = {purpose: [ref for ref in rows if not isinstance(ref, dict) or ref.get('document_id') not in previous_ids]
                          if isinstance(rows, list) else rows for purpose, rows in current.conditions.items()}
            if conditions != current.conditions:
                current.conditions = conditions
                current.save(using=using, update_fields=['conditions'])
                tender.conditions = conditions
        if previous is None or previous.pk != preview.pk:
            mark_tender_inputs_changed(tender)
        return preview


def record_document_error(tender, url, message):
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = prepare_tender_for_write(tender)
        preview = document_preview_map(current, [url], using=using).get(url)
        if preview is not None:
            link, _ = TenderDocument.objects.using(using).get_or_create(tender=current, source_key=url, preview=preview)
            link.last_error = str(message)
            link.save(using=using, update_fields=['last_error', 'updated_at'])


def publish_analysis(tender, *, kind, input_fingerprint, result, basis=None, commercial_item=None, component=None, job=None):
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = prepare_tender_for_write(tender)
        previous = TenderAnalysis.objects.using(using).filter(
            tender=current, kind=kind, commercial_item=commercial_item, component=component, is_current=True,
        ).first()
        if previous is None:
            previous = TenderAnalysis.objects.using(using).filter(tender=current, kind=kind,
                commercial_item=commercial_item, component=component).order_by('-pk').first()
        basis = basis if basis is not None else []
        if previous and previous.is_current and previous.input_fingerprint == input_fingerprint and previous.result == result and previous.basis == basis:
            return previous
        analysis = TenderAnalysis(tender=current, kind=kind, input_fingerprint=input_fingerprint, result=result,
                                  basis=basis, commercial_item=commercial_item, component=component, job=job,
                                  supersedes=previous, is_current=False)
        analysis._state.db = using
        analysis.full_clean()
        if previous:
            previous.is_current = False
            previous.save(using=using, update_fields=['is_current'])
        analysis.is_current = True
        analysis.full_clean()
        analysis.save(using=using)
        return analysis


def save_analysis_projection(tender, *, kind, values, input_fingerprint, basis=None, expected_title=None, expected_inputs=None):
    """Legacy read fields are projections; publication and projection commit together."""
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = Tender.objects.using(using).select_for_update().get(pk=tender.pk)
        if expected_inputs is not None:
            from tender_selection.services import _risk_input_fingerprint, _document_input_fingerprint
            actual_inputs = _risk_input_fingerprint(current) if kind == 'risk' else _document_input_fingerprint(current)
            if actual_inputs != expected_inputs:
                return False
        if expected_title is not None and (current.title != expected_title or current.status != Tender.NEW or current.review != Tender.UNREVIEWED):
            return False
        if kind == 'risk':
            result = values['risk_assessment']
        elif kind == 'profile':
            result = {key: value for key, value in values['profile_assessment'].items() if key != 'billing'}
        else:
            raise ValueError('Неизвестная проекция анализа.')
        if isinstance(result, dict) and result.get('stale'):
            invalidate_analysis(current, kind=kind)
        else:
            publish_analysis(current, kind=kind, input_fingerprint=input_fingerprint, result=result, basis=basis)
        for field, value in values.items():
            setattr(current, field, value)
            setattr(tender, field, value)
        current.save(using=using, update_fields=list(values))
        tender.data_version = current.data_version
        return True


def invalidate_analysis(tender, *, kind):
    using = tender._state.db or DEFAULT_DB_ALIAS
    TenderAnalysis.objects.using(using).filter(tender=tender, kind=kind, is_current=True).update(is_current=False)


def mark_tender_inputs_changed(tender, *, commercial_ids=()):
    """Keep old results; require verification without scheduling any paid work."""
    from .models import ComponentRoutePlan
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = Tender.objects.using(using).select_for_update().get(pk=tender.pk)
        if current.status == Tender.DISMISSED:
            return
        if current.risk_assessment and not current.risk_assessment.get('stale'):
            saved = TenderAnalysis.objects.using(using).filter(tender=current, kind='risk', commercial_item__isnull=True, component__isnull=True).order_by('-pk').first()
            if saved is None or saved.result != current.risk_assessment:
                historical = TenderAnalysis(tender=current, kind='risk', result=deepcopy(current.risk_assessment),
                    is_current=False, supersedes=saved, basis=[{'legacy_checked_at': current.risk_checked_at.isoformat() if current.risk_checked_at else None}])
                historical.save(using=using)
            data = deepcopy(current.risk_assessment)
            data['stale'] = True
            current.risk_assessment = data
            current.save(using=using, update_fields=['risk_assessment'])
        invalidate_analysis(current, kind='risk')
        tender.risk_assessment = current.risk_assessment
        if commercial_ids:
            ComponentRoutePlan.objects.using(using).filter(commercial_item__tender=current, commercial_item_id__in=commercial_ids,
                status__in=['planned', 'ready', 'needs_review']).update(status='stale')


def notification_source_fingerprint(source):
    """Business input only: presentation labels never create notice revisions."""
    from .source_memory import signature
    national = source.metadata.get('notification_national_regime') or {}
    quantity = source.quantity.normalize() if source.quantity is not None else None
    return signature([source.original_text, str(quantity), source.unit, source.requirements,
                      source.metadata.get('code') or '',
                      national.get('status', 'unknown'), national.get('basis') or []])


def reconcile_duplicate_notice_items(tender, *, apply=False):
    """Retire only equal accepted groups from equal versions of one notice row."""
    from collections import defaultdict
    from django.utils import timezone
    from .models import TenderSourceItem, TenderCommercialItem, CalculationComponent, TenderComputeJob
    from .source_memory import signature
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        owners = Tender.objects.using(using)
        current = (owners.select_for_update() if apply else owners).get(pk=tender.pk)
        if apply and current.status == Tender.DISMISSED:
            raise ValueError('Сначала верните тендер из архива в работу.')
        rows = list(TenderSourceItem.objects.using(using).filter(tender=tender).order_by('pk'))
        groups = defaultdict(list)
        for row in rows:
            if row.source_type == 'notification' and not row.metadata.get('duplicate_of'):
                groups[(row.source_key.split(':v:')[0], notification_source_fingerprint(row))].append(row)
        aliases, retired, conflicts = {}, {}, []
        def content(row):
            return signature([row.original_text, str(row.quantity.normalize()) if row.quantity is not None else None,
                              row.unit, row.requirements,
                              {key: row.metadata.get(key) for key in ('relationship', 'quantity_per_parent', 'supply_kind', 'national_regime')}])
        for roots in groups.values():
            accepted = [(root, [row for row in rows if row.parent_id == root.pk and row.is_active
                               and row.source_type == 'document_extraction']) for root in roots]
            accepted = [(root, children) for root, children in accepted if children]
            if len(accepted) < 2:
                continue
            contents = [{content(row): row for row in children} for _, children in accepted]
            if any(len(keys) != len(children) for keys, (_, children) in zip(contents, accepted)) or any(set(keys) != set(contents[0]) for keys in contents[1:]):
                conflicts.append([root.pk for root, _ in accepted])
                continue
            def decision(group):
                return max((event.get('at', '') for row in group[1] for event in row.metadata.get('review_history', [])
                            if event.get('decision') == 'accept'), default=''), group[0].pk
            keep_root, keep_children = max(accepted, key=decision)
            kept = {content(row): row for row in keep_children}
            for root, children in accepted:
                if root.pk == keep_root.pk:
                    continue
                aliases[root.pk] = keep_root.pk
                retired.update({row.pk: kept[content(row)].pk for row in children})
        plan = {'root_aliases': aliases, 'source_aliases': retired, 'conflicting_groups': conflicts}
        if apply and retired:
            from tender_selection.services import _document_input_fingerprint
            previous_input = _document_input_fingerprint(tender)
            goods = list(TenderCommercialItem.objects.using(using).filter(tender=tender, source_items__pk__in=retired).distinct())
            for good in goods:
                if set(good.source_items.values_list('pk', flat=True)) - retired.keys():
                    raise ValueError('Повтор связан с общим рабочим товаром; требуется отдельная проверка.')
            for row in rows:
                replacement = retired.get(row.pk) or aliases.get(row.pk)
                if replacement:
                    row.is_active = False
                    row.metadata = {**row.metadata, 'duplicate_of': replacement, 'duplicate_retired_at': timezone.now().isoformat()}
                    row.save(using=using, update_fields=['is_active', 'metadata'])
            ids = [good.pk for good in goods]
            CalculationComponent.objects.using(using).filter(commercial_item_id__in=ids).update(status='superseded')
            TenderCommercialItem.objects.using(using).filter(pk__in=ids).update(status='superseded')
            # Same evidence and same accepted values: only duplicate identities disappear.
            current_input = _document_input_fingerprint(tender)
            for job in TenderComputeJob.objects.using(using).filter(tender=tender, diagnostics__document_input_fingerprint=previous_input):
                job.diagnostics = {**job.diagnostics, 'document_input_fingerprint': current_input,
                                   'duplicate_source_aliases': {str(key): value for key, value in retired.items()}}
                job.save(using=using, update_fields=['diagnostics', 'updated_at'])
        return plan
