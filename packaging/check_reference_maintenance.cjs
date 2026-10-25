/* Latest frozen installer maintains an older trusted active release after rollback.
   Usage: URL_FILE EVIDENCE_DIR OLD_INACTIVE_OPERATION EXPECTED_ACTIVE_RELEASE */
const fs = require('fs');
const path = require('path');
const {chromium} = require('playwright');

async function main() {
  const [urlFile, evidenceDir, oldOperation, expectedRelease] = process.argv.slice(2);
  if (!expectedRelease) throw new Error('Missing explicit maintenance scenario');
  const url = fs.readFileSync(urlFile, 'utf8').trim(), parsed = new URL(url);
  if (parsed.hostname !== '127.0.0.1') throw new Error('Only loopback allowed');
  const token = new URLSearchParams(parsed.hash.slice(1)).get('token');
  fs.mkdirSync(evidenceDir, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  async function api(suffix) {
    const response = await page.request.get(parsed.origin + '/api/v1' + suffix, {headers: {'X-HInstall-Token': token}});
    const value = await response.json();
    if (!response.ok() || value.error) throw new Error('API read failed');
    return value.result;
  }
  try {
    await page.goto(url);
    await page.waitForSelector('#pending-operations:not([hidden])', {timeout: 30000});
    const pending = await api('/operations?unfinished_only=true');
    const index = pending.operations.findIndex(x => x.operation_id === oldOperation);
    if (index < 0 || pending.operations[index].status !== 'RECOVERY_REQUIRED') throw new Error('Expected inactive operation not found');
    await page.locator('#pending-list button').nth(index).click();
    await page.waitForSelector('[data-panel="4"]:visible', {timeout: 30000});
    await page.locator('#cancel-paused').click();
    await page.waitForFunction(() => JSON.parse(document.querySelector('#result-details').textContent).status === 'CANCELED');
    const closed = JSON.parse(await page.locator('#result-details').textContent());
    if (closed.operation_id !== oldOperation || closed.result.effects_undone !== false) throw new Error('Inactive cancellation changed identity or asserted rollback');
    await page.screenshot({path: path.join(evidenceDir, 'cancel-inactive.png'), fullPage: true});
    const actions = [];
    async function action(name, locale) {
      await page.locator('#maintenance').click();
      await page.selectOption('#language', locale);
      await page.selectOption('#action', name);
      if (name === 'backup') await page.locator('#archive-password').fill('reference-test-only-archive-password');
      await page.locator('#next').click();
      await page.waitForSelector('[data-panel="1"]:visible', {timeout: 30000});
      await page.locator('#terms').evaluate(e => {e.scrollTop=e.scrollHeight;e.dispatchEvent(new Event('scroll'));});
      await page.locator('#accept-terms').check();
      await page.locator('#next').click();
      await page.waitForSelector('[data-panel="2"]:visible', {timeout: 30000});
      const plan = JSON.parse(await page.locator('#plan-details').textContent());
      if (plan.engine_plan?.effective_active?.release !== expectedRelease) throw new Error('Plan lacks trusted active release binding');
      await page.locator('#accept-plan').check();
      await page.locator('#next').click();
      await page.waitForSelector('[data-panel="4"]:visible', {timeout: 180000});
      const result = JSON.parse(await page.locator('#result-details').textContent());
      if (result.status !== 'SUCCEEDED') throw new Error(name + ': ' + JSON.stringify(result.error || result.result));
      actions.push({action: name, locale, status: result.status, operation_id: result.operation_id,
        effective_release: plan.engine_plan.effective_active.release});
      await page.screenshot({path:path.join(evidenceDir, name+'-'+locale+'.png'),fullPage:true});
      return result;
    }
    await action('stop', 'de');
    await action('start', 'en');
    const health = await (await page.request.get('http://127.0.0.1:18480/healthz')).json();
    if (!health.ready || health.release !== expectedRelease || health.product_version !== '2.0.0' || health.database_schema !== 2)
      throw new Error('Wrong active product actually started');
    const saved = await action('backup', 'ru');
    if (saved.result.archive?.release !== expectedRelease || saved.result.archive?.encrypted !== true)
      throw new Error('Backup is not bound to active release');
    await action('stop', 'en');
    if (errors.length) throw new Error('Browser errors');
    const output = {scope:'Linux x64 frozen latest bootstrap after rollback',
      old_operation:oldOperation,inactive_attempt_canceled:true,actions,actual_health:health,
      archive_sha256:saved.result.archive.sha256,browser_errors:errors,product_stopped:true,production_ready:false};
    fs.writeFileSync(path.join(evidenceDir,'result.json'),JSON.stringify(output,null,2)+'\n');
    console.log(JSON.stringify(output));
  } catch(error) {
    await page.screenshot({path:path.join(evidenceDir,'failure.png'),fullPage:true});
    console.error(JSON.stringify({error:error.message,wizard_error:await page.locator('#error').textContent()}));
    process.exitCode=1;
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
