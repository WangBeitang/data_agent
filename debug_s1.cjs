const { chromium } = require('playwright');

const QUERY = process.argv[2] || '为什么2025年2月销售额较1月明显下降？';
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
  console.log('PAGE LOADED');

  // type query into the chat input
  const input = await page.waitForSelector('.input-box textarea, .input-box input, textarea', { timeout: 15000 });
  await input.click();
  await input.fill(QUERY);
  await input.press('Enter');
  console.log('QUERY SUBMITTED @', new Date().toISOString());

  const deadline = Date.now() + 420000;
  let lastDump = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(20000);
    const snap = await page.evaluate(() => {
      const txt = document.body.innerText || '';
      const q = (sel) => document.querySelectorAll(sel).length;
      // route badge: look for the el-route or .route-badge text
      let routeBadge = '';
      const rb = document.querySelector('.route-badge');
      if (rb) routeBadge = rb.innerText.trim();
      // loading state: send button disabled?
      const btn = document.querySelector('.input-box button');
      const loading = btn ? btn.disabled : null;
      return {
        len: txt.length,
        tlItem: q('.tl-item'),
        actionItem: q('.action-item'),
        queryBlock: q('.query-block'),
        queryTable: q('.query-table'),
        contribution: q('.contribution-chart'),
        evidence: q('.evidence-card'),
        copyBtn: (txt.includes('复制报告')),
        routeBadge,
        loading,
        has109030: txt.includes('109030.5'),
        has80009: txt.includes('80009.0'),
        hasDelta: txt.includes('-29021.5'),
        hasRate: txt.includes('-26.62') || txt.includes('26.62'),
        hasDrivers: txt.includes('drivers') || txt.includes('驱动'),
        hasOffsets: txt.includes('offsets') || txt.includes('抵消'),
        hasBreakdown: txt.includes('breakdown') || txt.includes('拆解') || txt.includes('维度'),
        hasBoundary: txt.includes('数据边界') || txt.includes('边界'),
        hasSuggest: txt.includes('建议'),
        forbidden: /password|api_key|sk-[A-Za-z0-9]|traceback|数据库密码/i.test(txt),
      };
    });
    const t = ((420000 - (deadline - Date.now())) / 1000).toFixed(0);
    console.log(`T+${t}s len=${snap.len} route="${snap.routeBadge}" load=${snap.loading} tl=${snap.tlItem} act=${snap.actionItem} qb=${snap.queryBlock} qt=${snap.queryTable} contrib=${snap.contribution} ev=${snap.evidence} copy=${snap.copyBtn} 109030=${snap.has109030} 80009=${snap.has80009} delta=${snap.hasDelta} rate=${snap.hasRate} drv=${snap.hasDrivers} off=${snap.hasOffsets} bk=${snap.hasBreakdown} bnd=${snap.hasBoundary} sug=${snap.hasSuggest} forb=${snap.forbidden}`);
    if (snap.routeBadge && snap.loading === false && (snap.queryTable > 0 || snap.evidence > 0)) {
      console.log('COMPLETION DETECTED');
      lastDump = snap;
      break;
    }
  }

  // final full dump
  const full = await page.evaluate(() => document.body.innerText || '');
  console.log('===== FULL BODY TEXT (last 4000 chars) =====');
  console.log(full.slice(-4000));
  console.log('===== CONSOLE (' + consoleMsgs.length + ') =====');
  console.log(consoleMsgs.slice(0, 60).join('\n'));
  console.log('===== PAGE ERRORS (' + pageErrors.length + ') =====');
  console.log(pageErrors.slice(0, 20).join('\n'));
  console.log('===== FAILED REQUESTS (' + failedReqs.length + ') =====');
  console.log(failedReqs.slice(0, 20).join('\n'));

  await page.screenshot({ path: '/tmp/debug_s1.png', fullPage: true });
  await browser.close();
  console.log('DONE');
})().catch(e => { console.error('SCRIPT ERROR:', e); process.exit(1); });
