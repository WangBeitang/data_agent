const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:5173';
const OUT = '/Users/beitang/Desktop/项目实战/data_agent/acceptance_results.json';
const FORBIDDEN = ['password', 'api_key', 'apikey', 'sk-', 'traceback', 'Prompt', 'hidden reasoning', '数据库密码'];

function count(page, sel) { return page.$$eval(sel, (e) => e.length).catch(() => 0); }
function exists(page, sel) { return page.$(sel).then((e) => !!e).catch(() => false); }
function texts(page, sel) { return page.$$eval(sel, (e) => e.map((x) => x.textContent.trim())).catch(() => []); }

async function runScenario(query, idx) {
  const consoleMsgs = [];
  const pageErrors = [];
  const browser = await chromium.launch({ channel: 'chrome', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 1500 } });
  const page = await context.newPage();
  page.on('console', (m) => consoleMsgs.push(`[${m.type()}] ${m.text()}`));
  page.on('pageerror', (e) => pageErrors.push(String(e && e.stack ? e.stack : e)));

  // Hook the SSE stream at the fetch layer to capture real completion.
  await page.addInitScript(() => {
    window.__events = [];
    const origFetch = window.fetch.bind(window);
    window.fetch = async (url, opts) => {
      const res = await origFetch(url, opts);
      if (url && url.toString().includes('/api/query') && res.body) {
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        const pump = async () => {
          const { value, done } = await reader.read();
          if (done) return;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const block = buf.slice(0, i); buf = buf.slice(i + 2);
            const line = block.trim();
            if (line.startsWith('data:')) {
              try { window.__events.push(JSON.parse(line.replace(/^data:\s*/, ''))); } catch (e) {}
            }
            await pump();
          }
        };
        pump();
        // return the original (already-consumed?) — we cloned above via getReader,
        // so the app's own reader would be empty. Instead, reconstruct a new stream.
        const newStream = new ReadableStream({
          start(controller) {
            const enc = new TextEncoder();
            const r2 = res.clone().body.getReader();
            const d2 = new TextDecoder();
            let b2 = '';
            const p2 = async () => {
              const { value, done } = await r2.read();
              if (done) { controller.close(); return; }
              controller.enqueue(value);
              b2 += d2.decode(value, { stream: true });
              let j;
              while ((j = b2.indexOf('\n\n')) >= 0) {
                const blk = b2.slice(0, j); b2 = b2.slice(j + 2);
                const ln = blk.trim();
                if (ln.startsWith('data:')) {
                  try { window.__events.push(JSON.parse(ln.replace(/^data:\s*/, ''))); } catch (e) {}
                }
                await p2();
              }
            };
            p2();
          },
        });
        return new Response(newStream, { headers: res.headers, status: res.status, statusText: res.statusText });
      }
      return res;
    };
  });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  const input = await page.$('input[placeholder*="请输入"]');
  if (!input) { await browser.close(); return { query, error: 'NO_INPUT' }; }
  await input.click();
  await input.fill(query);
  await input.press('Enter');

  // Wait for the REAL done event captured by our hook.
  let doneEvent = null;
  try {
    await page.waitForFunction(() => {
      const evs = window.__events || [];
      return evs.some((e) => e && e.type === 'done');
    }, { timeout: 420000 });
    doneEvent = await page.evaluate(() => (window.__events || []).filter((e) => e && e.type === 'done').slice(-1)[0]);
  } catch (e) {}

  await page.waitForTimeout(3000);

  const routeBadge = await texts(page, '.route-badge .badge');
  const hasReport = await exists(page, '.attr-report');
  const hasTimeline = await exists(page, '.analysis-timeline');
  const actionCount = await count(page, '.tl-action');
  const queryBlockCount = await count(page, '.tl-query');
  const sqlCount = await count(page, '.tl-sql');
  const resultTableCount = await count(page, '.result-table');
  const hasContribution = await exists(page, '.contribution-chart');
  const evidenceCount = await count(page, '.rp-evidence');
  const hasCopy = await exists(page, '.rp-copy');
  const bodyText = await page.evaluate(() => document.body.innerText);
  const forbiddenFound = FORBIDDEN.filter((f) => bodyText.includes(f));
  const consoleForbidden = FORBIDDEN.filter((f) => consoleMsgs.some((m) => m.toLowerCase().includes(f.toLowerCase())));
  const capturedEvents = await page.evaluate(() => (window.__events || []).map((e) => e.type));

  const shot = `/Users/beitang/Desktop/项目实战/data_agent/acceptance_scenario_${idx}.png`;
  await page.screenshot({ path: shot, fullPage: true });
  await browser.close();

  return {
    query, idx,
    doneEvent,
    capturedEventTypeCounts: capturedEvents.reduce((a, t) => (a[t] = (a[t] || 0) + 1, a), {}),
    routeBadge,
    hasReport, hasTimeline,
    actionCount, queryBlockCount, sqlCount, resultTableCount,
    hasContribution, evidenceCount, hasCopy,
    forbiddenFound, consoleForbidden,
    consoleErrors: consoleMsgs.filter((m) => m.startsWith('[error]')),
    pageErrors,
    bodyTextLen: bodyText.length,
    shot,
    bodyText,
  };
}

(async () => {
  const arg = process.argv[2];
  const idx = parseInt(process.argv[3] || '1', 10);
  let all = [];
  try { all = JSON.parse(fs.readFileSync(OUT, 'utf8')); } catch (e) { all = []; }
  const r = await runScenario(arg, idx);
  const i = all.findIndex((x) => x.idx === idx);
  if (i >= 0) all[i] = r; else all.push(r);
  fs.writeFileSync(OUT, JSON.stringify(all, null, 2));
  process.stdout.write(`SCENARIO ${idx} DONE (doneEvent=${!!r.doneEvent}, errors=${r.consoleErrors.length + r.pageErrors.length})\n`);
})().catch((e) => { process.stderr.write('SCRIPT_ERROR: ' + (e && e.stack ? e.stack : String(e)) + '\n'); process.exit(1); });
