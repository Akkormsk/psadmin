"""Read-only labels for tender navigation; no new readiness or calculation rules."""


def document_readiness(documents, diagnostics):
    prepared = (((diagnostics or {}).get('stages') or {}).get('documents') or {}).get('result') or {}
    considered = prepared.get('documents') or []
    statuses = {row.get('url'): row.get('reading_status') for row in considered if row.get('url')}
    current = [document.get('url') for document in documents]
    return {
        'read': sum(statuses.get(url) == 'read' for url in current),
        'total': len(documents),
        'known': bool(current) and all(url and url in statuses for url in current),
    }


def result_badge(estimate, verdict):
    if estimate is None:
        return {'label': 'Нет расчёта', 'kind': 'neutral'}
    if (estimate.summary_snapshot or {}).get('is_incomplete', True):
        return {'label': 'Частичный', 'kind': 'warning'}
    status = (verdict or {}).get('input_status') or {}
    if status.get('state') == 'stale':
        return {'label': 'Проверить расчёт', 'kind': 'warning'}
    if status.get('state') != 'current' or verdict.get('roi') is None:
        return {'label': 'Проверить расчёт', 'kind': 'neutral'}
    return {'roi': verdict['roi'], 'kind': verdict['roi_state']}
