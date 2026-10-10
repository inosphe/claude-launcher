/* Exercise the terminal answer strip with the real app, DOM, CSS and HTTP.
   All APIs are fixtures; no live workflow is answered. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const row = { cwd: '/repo', scope: 'demo', run: 'header-run', workflow: 'improv-worker-v2',
  status: 'waiting_approval', gated: true, owed: ['ask: ask-pose'], answers: [
    { kind: 'ask', instance: 'round/ask-pose@40', label: 'ask-pose',
      prompt: 'End the session?', recommended: 'end', require_reason: true,
      options: [{ name: 'end' }, { name: 'continue' }, { name: 'reframe' }] },
  ] };
const posts = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname === '/api/batch') { res.statusCode = 404; return res.end('{}'); }
    if (req.method === 'POST' && /^\/api\/cflow2?\//.test(url.pathname)) {
      let body = '';
      for await (const part of req) body += part;
      posts.push({ path: url.pathname, body: JSON.parse(body) });
      row.gated = false;
      row.answers = [];
    }
    let data = {};
    if (url.pathname === '/api/daemon') data = { version: 'fixture', boot_id: 'header' };
    if (url.pathname === '/api/sessions') data = { sessions: [] };
    if (url.pathname === '/api/cflow2/rail') data = { rows: { demo: row } };
    if (url.pathname === '/api/observer') data = { sessions: [], enabled: false };
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/static\//, ''));
  try {
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => booted && cflow2RailCache.demo);
    // Show the attached-session surface without opening a real PTY socket.
    await page.evaluate(() => {
      currentPage = 'terminal';
      currentName = 'demo';
      document.getElementById('home-view').classList.add('hidden');
      document.getElementById('term-header').classList.remove('hidden');
      renderTermCflow();
    });
    const end = page.locator('#term-cflow').getByRole('button', { name: 'end', exact: true });
    assert.equal(await end.evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(35, 134, 54)',
      'recommended end retains the run page approval colour');
    assert.match(await end.getAttribute('title'), /recommended/);
    const dialog = page.waitForEvent('dialog');
    const click = end.click();
    const confirm = await dialog;
    assert.equal(confirm.type(), 'confirm');
    assert.equal(confirm.message(), "Answer 'end' to ask-pose?");
    const sent = page.waitForResponse(r => r.url().endsWith('/api/cflow2/approve'));
    await confirm.accept();
    await click;
    await sent;
    assert.deepEqual(posts, [{ path: '/api/cflow2/approve', body: {
      cwd: '/repo', scope: 'demo', node: 'round/ask-pose@40', decision: 'end', reason: '',
    } }]);
    await page.waitForFunction(() => document.getElementById('term-cflow').classList.contains('hidden'));
    assert.deepEqual(errors, []);
    console.log('termcflow_browser: recommended end colour, click, confirmation, POST and refresh passed');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exit(1); });
