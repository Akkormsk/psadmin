"""Shared writes for new/active tenders; legacy archive reads stay side-effect free."""
from django.db import DEFAULT_DB_ALIAS, transaction

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
        basis = basis if basis is not None else []
        if previous and previous.input_fingerprint == input_fingerprint and previous.result == result and previous.basis == basis:
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


def save_analysis_projection(tender, *, kind, values, input_fingerprint, basis=None, expected_title=None):
    """Legacy read fields are projections; publication and projection commit together."""
    using = tender._state.db or DEFAULT_DB_ALIAS
    with transaction.atomic(using=using):
        current = Tender.objects.using(using).select_for_update().get(pk=tender.pk)
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
