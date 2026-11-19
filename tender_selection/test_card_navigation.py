from types import SimpleNamespace

from django.test import SimpleTestCase

from .card_navigation import document_readiness, result_badge


class CardNavigationTests(SimpleTestCase):
    def test_document_readiness_matches_current_file_urls_and_not_old_files(self):
        documents = [{'url': 'current-a'}, {'url': 'current-b'}]
        diagnostics = {'stages': {'documents': {'result': {'documents': [
            {'url': 'current-a', 'reading_status': 'read'},
            {'url': 'current-b', 'reading_status': 'partial'},
            {'url': 'removed', 'reading_status': 'read'},
        ]}}}}
        self.assertEqual(document_readiness(documents, diagnostics), {'read': 1, 'total': 2, 'known': True})
        self.assertEqual(document_readiness(documents, {}), {'read': 0, 'total': 2, 'known': False})

    def test_roi_is_only_presented_as_current_when_inputs_are_current(self):
        estimate = SimpleNamespace(summary_snapshot={'is_incomplete': False})
        verdict = {'roi': 29, 'roi_state': 'warning', 'input_status': {'state': 'current'}}
        self.assertEqual(result_badge(estimate, verdict)['roi'], 29)
        verdict['input_status']['state'] = 'stale'
        self.assertNotIn('roi', result_badge(estimate, verdict))
        verdict['input_status']['state'] = 'unknown'
        self.assertNotIn('roi', result_badge(estimate, verdict))
        self.assertEqual(result_badge(None, None)['label'], 'Нет расчёта')
        estimate.summary_snapshot['is_incomplete'] = True
        self.assertEqual(result_badge(estimate, None)['label'], 'Частичный')

    def test_notice_links_and_default_tab_preserve_navigation_and_processing(self):
        from django.template.loader import render_to_string
        from html.parser import HTMLParser
        from .models import Tender

        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.attrs = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.attrs[attrs['id']] = attrs

        html = render_to_string('tender_selection/detail.html', {
            'tender': Tender(pk=7, title='Блокноты', law='fz44', max_price=25000,
                eis_url='https://example.org/notice'),
            'request': SimpleNamespace(user=SimpleNamespace(is_superuser=True, is_authenticated=False)),
            'display_purchase_number': '001', 'display_item_count': 2,
            'card': {'placing_way': 'Электронный аукцион', 'print_form_url': 'https://example.org/print'},
            'navigation_nmck': 25000,
            'navigation_documents': {'read': 1, 'total': 2, 'known': True},
            'navigation_result': {'label': 'Частичный', 'kind': 'warning'},
            'analysis_status': {'progress': {'steps': [
                {'label': name, 'state': 'complete'} for name in
                ('Извещение', 'Документы', 'Состав', '1875', 'ТЗ', 'Маршруты')]}},
        })
        nodes = Elements()
        nodes.feed(html)
        self.assertEqual(nodes.attrs['ts-tab-general']['aria-selected'], 'true')
        self.assertNotIn('hidden', nodes.attrs['ts-panel-general'])
        self.assertIn('hidden', nodes.attrs['ts-panel-products'])
        header_end = html.index('<section class="panel ts-tabs-workspace"')
        self.assertNotIn('https://example.org/notice', html[:header_end])
        self.assertIn('https://example.org/notice', html[header_end:])
        self.assertEqual(html.count('href="https://example.org/print"'), 1)
        self.assertLess(html.index('Электронный аукцион'), html.index('data-copy-notice="001"'))
        self.assertEqual(html.count('class="ts-processing-step"'), 6)
        self.assertEqual(html.count('role="tab"'), 5)
        self.assertIn('Частичный', html)
        self.assertNotIn('ROI %', html)
