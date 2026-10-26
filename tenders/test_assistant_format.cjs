const test = require('node:test');
const assert = require('node:assert/strict');
const {render} = require('../static/core/assistant-format.js');

test('table rows become labeled records without losing surrounding text', () => {
  const html = render('Перед списком\n\n| Дата | Сумма | Назначение |\n|---|---:|---|\n| 26.10.2026 | 600.00 | Упаковка |\n| 25.10.2026 | 1250.00 | Бумага |\n\nНичего не менялось.');
  assert.equal((html.match(/class="assistant-record"/g) || []).length, 2);
  for (const text of ['Дата', 'Сумма', 'Назначение', '600.00', '1250.00', 'Упаковка', 'Бумага', 'Перед списком', 'Ничего не менялось.']) assert.ok(html.includes(text));
  assert.ok(!html.includes('|---'));
});

test('model HTML remains inert in labels, values and ordinary text', () => {
  const html = render('<script>alert(1)</script>\n\n| <img src=x onerror=alert(1)> | Значение |\n|---|---|\n| <svg onload=alert(1)> | &" |');
  assert.ok(!/<script|<img|<svg/.test(html));
  assert.ok(html.includes('&lt;svg'));
  assert.ok(html.includes('&amp;&quot;'));
});

test('escaped pipes stay inside a cell', () => {
  const html = render('| Название | Значение |\n|---|---|\n| А \\| Б | 1 |');
  assert.ok(html.includes('А | Б'));
  assert.equal((html.match(/<dd>/g) || []).length, 2);
});

test('malformed tables and code examples remain plain text', () => {
  for (const text of ['| А | Б |\n|---|---|\n| 1 | 2 | 3 |', '```\n| А | Б |\n|---|---|\n| 1 | 2 |\n```']) {
    const html = render(text);
    assert.ok(!html.includes('assistant-record'));
    assert.ok(html.includes('| 1 | 2 |'));
  }
});

test('multiple tables and optional edge pipes preserve every row', () => {
  const html = render('А | Б\n---|---\n1 | 2\n\nТекст\n\n| В | Г |\n|---|---|\n| 3 | 4 |');
  assert.equal((html.match(/class="assistant-records"/g) || []).length, 2);
  assert.equal((html.match(/<dd>/g) || []).length, 4);
});
