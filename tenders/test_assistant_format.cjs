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

test('shared values appear once and record-specific fields stay in their rows', () => {
  const html = render('| Дата | Счёт | Тип | Сумма | Назначение |\n|---|---|---|---|---|\n| 26.10.2026 | Наличные | Расход | 600.00 | Упаковка |\n| 25.10.2026 | Наличные | Расход | 1250.00 | Бумага |');
  assert.ok(html.includes('assistant-records__common'));
  assert.equal((html.match(/Наличные/g) || []).length, 1);
  assert.equal((html.match(/Расход/g) || []).length, 1);
  assert.equal((html.match(/class="assistant-record"/g) || []).length, 2);
  assert.equal((html.match(/<dd>/g) || []).length, 8);
  for (const value of ['600.00', '1250.00', 'Упаковка', 'Бумага']) assert.ok(html.includes(value));
});

test('identical rows retain their count and empty values are not promoted', () => {
  const html = render('| Название | Статус |\n|---|---|\n| Пример | Готов |\n| Пример | Готов |');
  assert.equal((html.match(/class="assistant-record"/g) || []).length, 2);
  assert.equal((html.match(/Пример/g) || []).length, 2);
  const blank = render('| А | Б |\n|---|---|\n| Один | |\n| Два | |');
  assert.ok(!blank.includes('assistant-records__common'));
});

let chromium;
try { ({chromium} = require('playwright')); } catch (_) {}
test('compact field labels stay whole and columns align at narrow widths', {skip: !chromium}, async () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const browser = await chromium.launch({headless:true, ...(fs.existsSync('/usr/bin/chromium') ? {executablePath:'/usr/bin/chromium'} : {}), args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:700,height:900}});
    const table = '| Дата | Счёт | Тип | Сумма | Назначение |\n|---|---|---|---|---|\n' +
      ['бумага', 'упаковка', 'Пробная печать макетов', 'Печать образца с достаточно длинным названием'].map((value, i) =>
        `| ${26-i}.10.2026 | Наличные | Расход | ${100+i}.00 | ${value} |`
      ).join('\n');
    const css = fs.readFileSync(path.join(__dirname, '../static/core/index.css'), 'utf8');
    await page.setContent('<style>' + css + '</style><article class="assistant-message" style="max-width:none">' + render(table) + '</article>');
    for (const width of [240, 300, 400]) {
      await page.locator('article').evaluate((el, width) => { el.style.width = width + 'px'; }, width);
      const rows = await page.locator('.assistant-record').evaluateAll(records => records.map(record => {
        const fields = [...record.children];
        return {labelLines:fields[2].querySelector('dt').getClientRects().length,
          firstY:fields[0].getBoundingClientRect().top, lastY:fields[2].getBoundingClientRect().top,
          amountX:fields[1].getBoundingClientRect().left, overflow:record.scrollWidth>record.clientWidth};
      }));
      assert.ok(rows.every(row => row.labelLines===1 && row.lastY>row.firstY && !row.overflow));
      assert.ok(rows.every(row => Math.abs(row.amountX-rows[0].amountX)<1));
    }
  } finally { await browser.close(); }
});
