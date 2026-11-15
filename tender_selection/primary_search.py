"""Editable upstream filters, shared by the settings page and assistant executor."""
import re
import os
from datetime import datetime
from decimal import Decimal

from django import forms

from .filtering import parse_terms

STAGES = [('1', 'Приём заявок'), ('2', 'Работа комиссии'), ('3', 'Завершено'), ('4', 'Отменено')]
METHODS = [
    ('epNotificationEF2020', 'Электронный аукцион (44-ФЗ)'),
    ('epNotificationEZK2020', 'Электронные котировки (44-ФЗ)'),
    ('epNotificationEOK2020', 'Электронный конкурс (44-ФЗ)'),
    ('epNotificationEZP', 'Запрос предложений (44-ФЗ)'),
    ('purchaseNoticeAE', 'Электронный аукцион (223-ФЗ)'),
    ('purchaseNoticeZK', 'Запрос котировок (223-ФЗ)'),
    ('purchaseNoticeOK', 'Конкурс (223-ФЗ)'),
    ('purchaseNoticeEP', 'Единственный поставщик (223-ФЗ)'),
    ('purchaseNotice', 'Другой способ (223-ФЗ)'),
]
# Provider notification types, rather than a guessed grouping of procurement methods.
OTHER_METHODS = ['fcsNotification111', 'epNotificationEOK', 'epNotificationEOKD',
                 'epNotificationEOKOU', 'epNotificationEZK', 'purchaseNoticeOA', 'purchaseNoticeAE94FZ',
                 'purchaseNoticeAESMBO', 'purchaseNoticeKESMBO', 'purchaseNoticeZKESMBO',
                 'purchaseNoticeZPESMBO', 'pprf615NotificationPO', 'pprf615NotificationEF']
METHODS += [(x, x) for x in OTHER_METHODS]


class PrimaryFilterForm(forms.Form):
    stage = forms.MultipleChoiceField(label='Этап закупки', choices=STAGES, required=False,
                                     widget=forms.CheckboxSelectMultiple)
    max_price_le = forms.DecimalField(label='Максимальная НМЦК, ₽', required=False, min_value=0,
                                     max_digits=16, decimal_places=2)
    purchase_type = forms.MultipleChoiceField(label='Способ закупки', choices=METHODS, required=False,
                                            widget=forms.SelectMultiple)
    for prefix, label in [('published', 'Публикация'), ('updated', 'Обновление'), ('deadline', 'Окончание подачи')]:
        for bound, suffix in [('after', 'с'), ('before', 'до')]:
            locals()[f'{prefix}_{bound}'] = forms.DateTimeField(
                label=f'{label}: {suffix}', required=False,
                widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'step': '1'}, format='%Y-%m-%dT%H:%M:%S'),
                help_text='Часовой пояс сайта. Пусто — без ограничения.')
    del prefix, label, bound, suffix
    for key, label in [('customer', 'ИНН заказчика'), ('owner', 'ИНН владельца (44-ФЗ)'),
                       ('responsible', 'ИНН ответственного (44-ФЗ)'), ('placer', 'ИНН разместившего (223-ФЗ)')]:
        locals()[key] = forms.RegexField(label=label, regex=r'^\d{10}$', required=False)
    del key, label
    for key, label in [('purchase_number', 'Номер закупки'), ('plan_number', 'Номер плана'),
                       ('position_number', 'Позиция плана'), ('ikz', 'ИКЗ (44-ФЗ)')]:
        locals()[key] = forms.RegexField(label=label, regex=r'^\d+$', required=False)
    del key, label
    for key, label in [('contract_guarantee_amount', 'Обеспечение контракта, ₽'),
                       ('contract_guarantee_part', 'Обеспечение контракта, %')]:
        for bound, suffix in [('ge', 'от'), ('le', 'до')]:
            locals()[f'{key}_{bound}'] = forms.DecimalField(label=f'{label}: {suffix}', required=False,
                min_value=0, max_value=100 if key.endswith('part') else None,
                max_digits=16, decimal_places=2)
    del key, label, bound, suffix
    drug = forms.ChoiceField(label='Лекарства (44-ФЗ)', required=False,
                            choices=[('', 'Не ограничивать'), ('true', 'Да'), ('false', 'Нет')])
    medical_product = forms.ChoiceField(label='Медизделия (44-ФЗ)', required=False,
                                      choices=[('', 'Не ограничивать'), ('true', 'Да'), ('false', 'Нет')])
    currency_code = forms.ChoiceField(label='Валюта', required=False,
                                     choices=[('', 'Любая'), ('RUB', 'Рубли'), ('not_RUB', 'Кроме рублей')])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial = dict(self.initial)
        for name, field in self.fields.items():
            value = self.initial.get(name)
            if isinstance(field, forms.DateTimeField) and isinstance(value, str) and value:
                self.initial[name] = field.to_python(value)

    def clean(self):
        data = super().clean()
        earliest = datetime.fromisoformat(os.getenv('GOSPLAN_PUBLICATION_START', '2024-01-01T00:00:00+00:00'))
        for key in ('published_after', 'published_before'):
            if data.get(key) and data[key] < earliest:
                self.add_error(key, 'ГосПлан не предоставляет публикации раньше ' + earliest.date().isoformat())
        for low, high in [('published_after', 'published_before'), ('updated_after', 'updated_before'),
                          ('deadline_after', 'deadline_before'),
                          ('contract_guarantee_amount_ge', 'contract_guarantee_amount_le'),
                          ('contract_guarantee_part_ge', 'contract_guarantee_part_le')]:
            if data.get(low) is not None and data.get(high) is not None and data[low] > data[high]:
                self.add_error(high, 'Конец диапазона должен быть не меньше начала.')
        return data

    def serializable(self):
        result = {}
        for key, value in self.cleaned_data.items():
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = str(value)
            if value not in (None, '', []) or key == 'stage':
                result[key] = value
        return result


def validate_primary(value):
    if not isinstance(value, dict) or set(value) - set(PrimaryFilterForm.base_fields):
        raise ValueError('Неизвестные поля первичного фильтра')
    form = PrimaryFilterForm(value)
    if not form.is_valid():
        raise ValueError('; '.join(f'{form.fields[k].label}: {", ".join(v)}' for k, v in form.errors.items()))
    return form.serializable()


def word_pattern(words):
    """Literal, case-insensitive OR. Compounds are checked locally at step 4.

    Each compound contributes its first part upstream, a safe superset even on
    providers without regex lookahead. No input is treated as executable regex.
    """
    alternatives = word_alternatives(words)
    return '(?i)(?:' + '|'.join(alternatives) + ')' if alternatives else None


def word_alternatives(words):
    alternatives = []
    for term in parse_terms(words):
        root = term[0]
        if len(root) < 3:
            raise ValueError('Плюс-слово должно содержать не менее трёх символов до знака +.')
        literal = ''.join('[её]' if c == 'е' else re.escape(c) for c in root)
        if literal not in alternatives:
            alternatives.append(literal)
    return alternatives


def split_primary_requests(params, words, law):
    from urllib.parse import urlencode
    # HTTP request-line transport limit (nginx's usual 8 KiB), not a cap on
    # keywords or business coverage. Every alternative survives partitioning.
    limit = int(os.getenv('GOSPLAN_MAX_REQUEST_LINE_BYTES', '8192'))
    def request(group):
        return {**params, 'object_info': '(?i)(?:' + '|'.join(group) + ')'}
    def length(group):
        candidate = request(group)
        # prepare_discovery and the classifier job add these after grouping.
        candidate.setdefault('updated_after', '2000-01-01T00:00:00.000000+00:00')
        candidate.setdefault('updated_before', '2000-01-01T00:00:00.000000+00:00')
        candidate.setdefault('classifier', ['9' * 50])
        return len(('GET /' + law + '/purchases?' + urlencode(candidate, doseq=True)
                    + '&limit=100&skip=1000 HTTP/1.1\r\n').encode())
    if not params.get('object_info'):
        return [params]
    groups, group = [], []
    for alternative in word_alternatives(words):
        if length([alternative]) > limit:
            raise ValueError('Одно плюс-слово с выбранными фильтрами превышает допустимую длину запроса.')
        if group and length(group + [alternative]) > limit:
            groups.append(request(group))
            group = []
        group.append(alternative)
    if group:
        groups.append(request(group))
    return groups


def build_primary_params(settings, law, now):
    filters = validate_primary(settings.primary_filters)
    # Provider rejects earlier dates (live HTTP 422); this is its retention boundary,
    # not a configurable business lookback or an implicit recent-publication window.
    params = {'sort': 'published_at_desc', 'published_after': os.getenv('GOSPLAN_PUBLICATION_START', '2024-01-01T00:00:00+00:00'),
              'published_before': now.replace(microsecond=0).isoformat(), 'max_price_ge': str(settings.min_price)}
    if settings.regions:
        params['region'] = [int(r) for r in settings.regions]
    pattern = word_pattern(settings.include_words)
    if pattern:
        params['object_info'] = pattern
    only44 = {'owner', 'responsible', 'ikz', 'drug', 'medical_product',
              'contract_guarantee_amount_ge', 'contract_guarantee_amount_le',
              'contract_guarantee_part_ge', 'contract_guarantee_part_le'}
    for key, value in filters.items():
        if key in only44 and law != 'fz44' or key == 'placer' and law != 'fz223':
            continue
        if key == 'currency_code' and value == 'not_RUB':
            key, value = 'currency_code_not', 'RUB'
        if key.startswith('deadline_'):
            key = ('collecting_finished_' if law == 'fz44' else 'submission_close_') + key.split('_')[-1]
        if value != []:
            params[key] = value == 'true' if key in {'drug', 'medical_product'} else value
    return params


def assistant_primary_argument():
    return {"type": "object", "required": False, "label": "Первичные фильтры ГосПлана",
            "description": "Изменяются только переданные поля. Пустое значение снимает ограничение; stage=[] означает все этапы.",
            "properties": {name: {"type": "string[]" if isinstance(field, forms.MultipleChoiceField)
                                          else "number" if isinstance(field, forms.DecimalField) else "string",
                                  "required": False, "label": str(field.label)}
                           for name, field in PrimaryFilterForm.base_fields.items()}}
