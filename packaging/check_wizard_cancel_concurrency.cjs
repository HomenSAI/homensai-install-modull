/* Browser contract: paused cancel can observe a concurrently resumed worker.
   Replays API snapshots only; does not claim real concurrent backend acceptance.
   Usage: URL_FILE EVIDENCE_DIR EXISTING_RECOVERABLE_OPERATION */
const fs = require('fs');
const path = require('path');
const {chromium} = require('playwright');

async function main() {
  const [urlFile, directory, operation] = process.argv.slice(2);
  if (!/^op_[a-zA-Z0-9_-]+$/.test(operation || '')) throw new Error('Explicit operation required');
  const url = fs.readFileSync(urlFile, 'utf8').trim(), parsed = new URL(url);
  if (parsed.hostname !== '127.0.0.1') throw new Error('Loopback only');
  const token = new URLSearchParams(parsed.hash.slice(1)).get('token');
  fs.mkdirSync(directory, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let release, requested = false, activePolls = 0;
  const canFinish = new Promise(resolve => {release = resolve;});
  async function read(suffix) {
    const response = await page.request.get(parsed.origin + '/api/v1' + suffix,
      {headers: {'X-HInstall-Token': token}});
    const value = await response.json();
    if (!response.ok() || value.error) throw new Error('Read-only API request failed');
    return value.result;
  }
  try {
    const prior = await read('/operations/' + operation);
    const paused = {...prior, status: 'RECOVERY_REQUIRED', cancel_requested: false};
    await page.route('**/*', async route => {
      const target = new URL(route.request().url());
      if (target.hostname !== '127.0.0.1') return route.abort();
      if (target.pathname === '/api/v1/operations/' + operation + '/cancel') {
        requested = true;
        return route.fulfill({json: {result: {...paused, status: 'RUNNING', cancel_requested: true}}});
      }
      if (target.pathname === '/api/v1/operations/' + operation + '/events') {
        if (requested) {
          activePolls += 1;
          await canFinish;
        }
        const snapshot = requested ? {...paused, status: 'CANCELED', cancel_requested: true} : paused;
        return route.fulfill({json: {result: {events: [], snapshot, next_sequence: 0}}});
      }
      return route.continue();
    });
    await page.goto(url);
    await page.waitForSelector('#pending-operations:not([hidden])', {timeout: 30000});
    const list = await read('/operations?unfinished_only=true&limit=100');
    const index = list.operations.findIndex(item => item.operation_id === operation);
    if (index < 0) throw new Error('Recoverable fixture missing');
    await page.locator('#pending-list button').nth(index).click();
    await page.waitForSelector('[data-panel="4"]:visible');
    await page.locator('#cancel-paused').click();
    await page.waitForSelector('[data-panel="3"]:visible');
    await page.waitForFunction(() => !document.querySelector('#cancel').closest('[data-panel]').hidden);
    release();
    await page.waitForSelector('[data-panel="4"]:visible');
    const result = JSON.parse(await page.locator('#result-details').textContent());
    if (result.status !== 'CANCELED' || activePolls < 1 || errors.length) throw new Error('Live-worker polling was lost');
    const unchanged = await read('/operations/' + operation);
    if (JSON.stringify(unchanged) !== JSON.stringify(prior)) throw new Error('Contract probe changed real operation');
    const report = {scope: 'Frozen GUI response-race contract with replayed API snapshots',
      status: 'PASS', live_response: 'RUNNING', returned_to_progress: true,
      active_polls: activePolls, final_status: result.status, real_operation_unchanged: true,
      browser_errors: errors, real_concurrent_backend: false, production_ready: false};
    fs.writeFileSync(path.join(directory, 'result.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally {
    release();
    await browser.close();
  }
}
main().catch(error => {console.error(error.message);process.exitCode = 1;});
