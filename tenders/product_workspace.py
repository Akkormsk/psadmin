"""Read-only presentation of an existing estimate in the tender product workspace."""
from django.urls import reverse


def estimate_workspace(estimate, user):
    if estimate is None or not (user.is_superuser or estimate.owner_id == user.pk):
        return None
    from .input_freshness import estimate_input_status
    freshness = estimate_input_status(estimate)
    lines = []
    for line in estimate.lines.all():
        lines.append({
            'id': line.pk, 'group_key': line.group_key, 'group_name': line.group_name,
            'name': line.name, 'quantity': str(line.quantity), 'nmck_unit': str(line.nmck_unit),
            'material_unit': str(line.material_unit), 'application_unit': str(line.application_unit),
            'logistics_unit': str(line.logistics_unit), 'product_url': line.product_url,
            'comment': line.comment, 'requirements': line.requirements,
        })
    return {
        'estimate_id': estimate.pk, 'lines': lines,
        'editable': user.is_superuser and estimate.status != 'finalized' and freshness['state'] != 'stale',
        'input_status': freshness,
        'save_url': reverse('tender_pipeline_estimate_save', args=[estimate.pk]),
        'fields': {
            'name': estimate.name, 'tender_number': estimate.tender_number,
            'owner_id': estimate.owner_id, 'reduction_percent': str(estimate.reduction_percent),
            'russia_delivery': str(estimate.russia_delivery), 'result_notes': estimate.result_notes,
        },
        'document_analysis': estimate.document_analysis,
    }
