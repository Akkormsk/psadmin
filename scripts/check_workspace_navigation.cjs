// Local synthetic browser check of the real shared workspace JavaScript. No LLM or business data.
const fs = require('fs');
const http = require('http');
const assert = require('assert/strict');
const {chromium} = require('playwright');
const base = fs.readFileSync('templates/base.html', 'utf8');
const start = base.lastIndexOf('<script>', base.indexOf('const embedded ='));
const script = base.slice(start + 8, base.indexOf('</script>', start));
const shell = content => `<!doctype html><body ${content.embedded ? 'class="workspace-embedded"' : ''}>${content.html}<div id="app-workspace-stack"></div><script>${script}</script></body>`;
const card = shell({embedded: true, html: `<main data-workspace-single-layer><h1>Тендер</h1>
${['analysis', 'proposal', 'trading'].map(action => `<form method="post" action="/${action}"><button name="action" value="accept">${action}</button><button name="action" value="reject">reject</button></form>`).join('')}
<form method="post" action="/transition" data-tender-transition><button>transition</button></form>
<a href="/calculation" data-workspace-open>Расчёт</a></main>`});
let posts = 0, rootLoads = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  res.setHeader('content-type', 'text/html; charset=utf-8');
  if (req.method === 'POST') {
    assert.equal(url.searchParams.get('workspace'), '1');
    posts++;
    if (url.pathname === "/transition") { res.setHeader("content-type", "application/json"); res.end(JSON.stringify({lifecycle_changed: true})); return; }
    res.writeHead(302, {location: '/card?workspace=1'}); res.end(); return;
  }
  if (url.pathname === '/') {
    rootLoads++;
    res.end(shell({html: '<a href="/card" data-workspace-open>Открыть тендер</a>'}));
  } else res.end(card);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const action of ['analysis', 'proposal', 'trading']) {
      await page.getByRole('link', {name: 'Открыть тендер', exact: true}).click();
      const frame = page.frameLocator('.app-workspace-layer__frame');
      const repetitions = action === 'proposal' ? 3 : 1;
      for (let i = 0; i < repetitions; i++) {
        await Promise.all([page.waitForResponse(response => response.request().method() === 'POST'), frame.getByRole('button', {name: action, exact: true}).click()]);
        await frame.getByRole('button', {name: action, exact: true}).waitFor();
      }
      assert.equal(await page.locator('.app-workspace-layer').count(), 1);
      assert.equal(await frame.locator('.app-workspace-layer').count(), 0);
      await page.locator('[data-workspace-close]').click();
      await page.waitForTimeout(150);
      assert.equal(await page.locator('.app-workspace-layer').count(), 0, action);
    }
    await page.getByRole('link', {name: 'Открыть тендер', exact: true}).click();
    const frame = page.frameLocator('.app-workspace-layer__frame');
    await frame.getByRole('link', {name: 'Расчёт', exact: true}).click();
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.app-workspace-layer').count(), 1);
    assert.ok(page.frames().some(frame => frame.url().includes('/calculation?workspace=1')));
    await page.locator('[data-workspace-close]').focus();
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.app-workspace-layer').count(), 0);
    await page.getByRole('link', {name: 'Открыть тендер', exact: true}).click();
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/' && response.request().method() === 'GET'),
      page.frameLocator('.app-workspace-layer__frame').getByRole('button', {name: 'transition', exact: true}).click(),
    ]);
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.app-workspace-layer').count(), 0);
    assert.equal(posts, 6);
    assert.equal(rootLoads, 2);
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log(`PASS: analysis, three position decisions, trading POST redirects, calculation navigation Escape and AJAX stage transition; one layer, ${posts} POSTs, ${rootLoads} collection loads`);
  } finally { await browser.close(); server.close(); }
})().catch(error => {console.error(error); server.close(); process.exit(1);});
