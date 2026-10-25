/* Frozen controller crash + new browser session + explicit GUI resume.
   Kills only the detached process group created by this test. Linux only.
   Usage: BUNDLE_EXE NEW_TEST_DIRECTORY */
const fs = require('fs');
const path = require('path');
const {spawn} = require('child_process');
const {chromium} = require('playwright');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const [exe, directory] = process.argv.slice(2);
  if (process.platform !== 'linux' || !path.isAbsolute(exe || '') || !path.isAbsolute(directory || ''))
    throw new Error('Linux test needs absolute bundle and fresh output paths');
  fs.mkdirSync(directory, {recursive: false, mode: 0o700});
  const root = path.join(directory, 'installed'), state = path.join(directory, 'state');
  let controller, context, page, launch = 0;
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  async function start() {
    const urlFile = path.join(directory, 'launch-' + (++launch) + '.url');
    controller = spawn(exe, ['--install-root', root, '--state-dir', state, '--no-browser', '--url-file', urlFile],
      {detached: true, stdio: ['ignore', 'ignore', 'ignore']});
    const deadline = Date.now() + 30000;
    while (!fs.existsSync(urlFile)) {
      if (controller.exitCode !== null || Date.now() > deadline) throw new Error('Controller did not start');
      await delay(25);
    }
    const url = fs.readFileSync(urlFile, 'utf8').trim();
    if (new URL(url).hostname !== '127.0.0.1') throw new Error('Only loopback allowed');
    context = await browser.newContext({viewport: {width: 1280, height: 1000}});
    page = await context.newPage();
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#product').textContent.includes('homensai-reference'));
    const parsed = new URL(url), token = new URLSearchParams(parsed.hash.slice(1)).get('token');
    return async (suffix, body) => {
      const response = await context.request.fetch(parsed.origin + '/api/v1' + suffix, {
        method: body ? 'POST' : 'GET', headers: {'X-HInstall-Token': token}, ...(body ? {data: body} : {})});
      const value = await response.json();
      if (!response.ok() || value.error) throw new Error('Installer API: ' + JSON.stringify(value.error));
      return value.result;
    };
  }
  async function stopController(signal) {
    if (controller && controller.exitCode === null && controller.signalCode === null) {
      const child = controller;
      process.kill(-child.pid, signal);
      await new Promise(resolve => child.once('exit', resolve));
    }
  }
  try {
    let api = await start();
    await page.selectOption('#language', 'ru');
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="1"]:visible', {timeout: 30000});
    await page.locator('#terms').evaluate(e => {e.scrollTop = e.scrollHeight;e.dispatchEvent(new Event('scroll'));});
    await page.locator('#accept-terms').check();
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="2"]:visible', {timeout: 30000});
    await page.locator('#accept-plan').check();
    const submitted = page.waitForResponse(r => r.url().endsWith('/api/v1/operations') && r.request().method() === 'POST');
    await page.locator('#next').click();
    const operation = (await (await submitted).json()).result.operation_id;
    let interruptedPhase = null;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      const stream = await api('/operations/' + operation + '/events');
      if (stream.snapshot.status !== 'RUNNING') throw new Error('Missed active operation; crash proof not obtained');
      const phases = (stream.events || []).filter(e => ['prepare', 'stage', 'migrate'].includes(e.phase));
      const latest = phases.at(-1);
      if (latest?.status === 'RUNNING') {interruptedPhase = latest.phase; break;}
      await delay(10);
    }
    if (!interruptedPhase) throw new Error('No active effect phase observed');
    await stopController('SIGKILL');
    await context.close();
    api = await start();
    await page.waitForSelector('#pending-operations:not([hidden])', {timeout: 30000});
    const pending = await api('/operations?unfinished_only=true');
    if (pending.operations.length !== 1 || pending.operations[0].operation_id !== operation
        || pending.operations[0].status !== 'RECOVERY_REQUIRED') throw new Error('Durable operation not found');
    await delay(300);
    if ((await api('/operations/' + operation)).status !== 'RECOVERY_REQUIRED') throw new Error('Unexpected automatic replay');
    await page.locator('#pending-list button').first().click();
    await page.waitForSelector('[data-panel="4"]:visible', {timeout: 30000});
    await page.screenshot({path: path.join(directory, 'recovery-review.png'), fullPage: true});
    await page.locator('#resume').click();
    await page.waitForSelector('[data-panel="3"]:visible', {timeout: 30000});
    await page.waitForSelector('[data-panel="4"]:visible', {timeout: 120000});
    const result = JSON.parse(await page.locator('#result-details').textContent());
    if (result.status !== 'SUCCEEDED' || result.operation_id !== operation) throw new Error('Resume failed: ' + JSON.stringify(result));
    const healthURL = process.env.HINSTALL_TEST_HEALTH_URL || 'http://127.0.0.1:18480/healthz';
    if (new URL(healthURL).hostname !== '127.0.0.1') throw new Error('Only loopback health URL allowed');
    const healthResponse = await context.request.get(healthURL);
    const health = await healthResponse.json();
    if (!healthResponse.ok() || health.ready !== true || health.product_version !== '1.0.0' || health.database_schema !== 1)
      throw new Error('Resumed product not ready');
    await page.screenshot({path: path.join(directory, 'resumed.png'), fullPage: true});
    const body = {locale: 'ru', mode: 'native', install_root: root, action: 'stop', product_modes: ['local']};
    const terms = await api('/terms/ru'), plan = await api('/plans', body);
    const refs = [...new Set([plan.review_consent_ref, ...(plan.required_consents || []), ...(plan.engine_plan?.required_consents || [])].filter(Boolean))];
    const stopped = await api('/operations', {plan_id: plan.plan_id, plan_digest: plan.plan_digest,
      consent_refs: refs, idempotency_key: require('crypto').randomUUID(), accepted_terms: true,
      accepted_plan: true, terms_digest: terms.digest, locale: 'ru'});
    const stopDeadline = Date.now() + 30000;
    let stopState;
    do {stopState = await api('/operations/' + stopped.operation_id); if (stopState.status === 'SUCCEEDED') break; await delay(50);} while (Date.now() < stopDeadline);
    if (stopState.status !== 'SUCCEEDED') throw new Error('Owned product cleanup failed');
    const output = {scope: 'Linux x64 frozen DEV controller crash + fresh browser GUI',
      interrupted_phase: interruptedPhase, same_operation_id: operation,
      recovery_status: 'RECOVERY_REQUIRED', automatic_replay: false, status: result.status,
      health, product_stopped: true, production_ready: false};
    fs.writeFileSync(path.join(directory, 'result.json'), JSON.stringify(output, null, 2) + '\n');
    console.log(JSON.stringify(output));
  } catch (error) {
    if (page && !page.isClosed()) await page.screenshot({path: path.join(directory, 'failure.png'), fullPage: true}).catch(() => {});
    console.error(JSON.stringify({error: error.message}));
    process.exitCode = 1;
  } finally {
    await stopController('SIGTERM');
    await browser.close();
  }
}
main().catch(error => {console.error(error.message);process.exitCode = 1;});
