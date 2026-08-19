const { chromium } = require('playwright');

// args: query  expectedSubstrings...
const QUERY = process.argv[2];
const EXPECT = process.argv.slice(3);
const URL = 'http://127.0.0.1:5173/';

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: [
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
      '--disable-background-timer-throttling',
      '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling',
    ],
  });
  const page = await browser.newPage();
  const consoleMsgs = [];
  const pageErrors = [];
  const failedReqs = [];
  page.on('console', m => consoleMsgs.push(`[${m.type()}] ${m.text()}`));
  page.on('pageerror', e => pageErrors.push(String(e)));
  page.on('requestfailed', r => failedReqs.push(`${r.url()} :: ${r.failure()?.errorText}`));

  await page.goto(URL, { waitUntil: 'networkidle', timeout: 30000 });
  await page.bringToFront();

  const input = await page.waitForSelector('.input-box textarea, .input-box input, textarea, input', { timeout: 15000 });
  await input.click();
  await input.fill(QUERY);
  await input.press('Enter');

  // wait until route badge appears and loading ends, or 7min cap
  let done = false;
  const deadline = Date.now() + 420000;
  while (Date.now() < deadline) {
    await page.bringToFront();
    await page.waitForTimeout(15000);
    const st = await page.evaluate(() => {
      const rb = document.querySelector('.route-badge');
      const btn = document.querySelector('.input-box button');
      return { route: rb ? rb.innerText.trim() : '', loading: btn ? btn.disabled : null,
               len: (document.body.innerText || '').length };
    });
    if (st.route && st.loading === false) { done = true; break; }
  }

  const full = await page.evaluate(() => document.body.innerText || '');
  const lower = full.toLowerCase();
  const checks = EXPECT.map(s => ({ s, found: full.includes(s) }));
  const consoleForbidden = consoleMsgs.filter(m => /password|api_key|sk-[A-Za-z0-9]|traceback|数据库密码/i.test(m));
  const bodyForbidden = /password|api_key|sk-[A-Za-z0-9]|traceback|数据库密码|Prompt/i.test(full);

  const result = {
    query: QUERY,
    completed: done,
    route: (await page.evaluate(() => { const rb=document.querySelector('.route-badge'); return rb?rb.innerText.trim():''; })),
    bodyLen: full.length,
    expectedChecks: checks,
    allExpectedFound: checks.every(c => c.found),
    consoleErrors: consoleMsgs.filter(m => m.startsWith('[error]')).length,
    pageErrors: pageErrors.length,
    failedReqs: failedReqs.length,
    consoleForbidden: consoleForbidden.length,
    bodyForbidden,
    hasCopy: full.includes('复制报告'),
    hasBoundary: full.includes('数据边界'),
    hasSuggest: full.includes('建议'),
  };
  console.log('=== RESULT ===');
  console.log(JSON.stringify(result, null, 2));
  console.log('=== EXPECTED NOT FOUND ===');
  console.log(checks.filter(c => !c.found).map(c => c.s).join('\n') || '(none)');
  console.log('=== FULL BODY (last 3500) ===');
  console.log(full.slice(-3500));
  console.log('=== FAILED REQS ===');
  console.log(failedReqs.join('\n') || '(none)');
  console.log('=== PAGE ERRORS ===');
  console.log(pageErrors.join('\n') || '(none)');

  await page.screenshot({ path: '/tmp/accept_last.png', fullPage: true });
  await browser.close();
})().catch(e => { console.error('SCRIPT ERROR:', e); process.exit(1); });
