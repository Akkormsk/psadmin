"""Staff-only read-only inspector of the tender working tree.

This is a diagnostic surface, not a separate item/route editor.
"""
import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from tender_selection.models import Tender
from .working_items import tender_working_items


def _pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _component_rows(components, depth=0):
    for component in components:
        yield {'node': component, 'indent_px': min(depth, 12) * 24}
        yield from _component_rows(component['components'], depth + 1)


@require_GET
@login_required
def tender_tree_debug(request, pk):
    """Render a non-mutating projection for TEST acceptance by staff users."""
    if not request.user.is_staff:
        raise PermissionDenied
    tender = get_object_or_404(Tender, pk=pk)
    tree = tender_working_items(tender)

    displayed = []
    issue_count = 0
    component_count = 0
    for item in tree['items']:
        rows = list(_component_rows(item['components']))
        historical_rows = list(_component_rows(item['historical_components']))
        component_count += len(rows) + len(historical_rows)
        issue_count += len(item.get('issues', []))
        issue_count += sum(len(row['node']['issues']) for row in rows + historical_rows)
        displayed.append({
            'node': item,
            'components': rows,
            'historical_components': historical_rows,
            'sources_json': _pretty(item['sources']),
            'requirements_json': _pretty(item['requirements']),
        })

    registry = [
        {'ref': ref, 'record': record, 'result_json': _pretty(record.get('result'))}
        for ref, record in tree['calculation_results'].items()
    ]
    return render(request, 'tenders/tender_tree_debug.html', {
        'tender': tender,
        'items': displayed,
        'registry': registry,
        'unattached': tree['unattached_calculations'],
        'component_count': component_count,
        'issue_count': issue_count,
        'unverified_count': sum(
            record['record']['freshness'] == 'unknown' for record in registry
        ),
        'navigation_parent_url': f'/tender-selection/{tender.pk}/',
    })
