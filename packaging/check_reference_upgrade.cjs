/* Actual frozen GUI maintenance acceptance. Explicit loopback URL only.
   Usage: URL_FILE EVIDENCE_DIR ACTION STATUS PRODUCT_VERSION DATABASE_SCHEMA */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const [urlFile, evidenceDir, action, expectedStatus, expectedVersion, expectedSchema] = process.argv.slice(2);
  if (!expectedSchema || !['update', 'rollback', 'repair'].includes(action)) throw new Error('Invalid scenario arguments');
  const url = fs.readFileSync(urlFile, 'utf8').trim();
  if (new URL(url).hostname !== '127.0.0.1') throw new Error('Only loopback allowed');
  fs.mkdirSync(evidenceDir, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  try {
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#product').textContent.includes('homensai-reference'));
    await page.selectOption('#language', process.env.HINSTALL_TEST_LOCALE || 'en');
    await page.selectOption('#action', action);
    await page.locator('#archive-password').fill('reference-test-only-archive-password');
    if (process.env.HINSTALL_TEST_ARCHIVE_PATH)
      await page.locator('#archive-path').fill(process.env.HINSTALL_TEST_ARCHIVE_PATH);
    if (action === 'rollback') await page.locator('#data-loss').check();
    const root = await page.locator('#install-root').inputValue();
    const preserved = path.join(root, 'data/user-files/acceptance-preserved.txt');
    const before = fs.readFileSync(preserved);
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="1"]:visible', {timeout: 30000});
    await page.locator('#terms').evaluate(e => {e.scrollTop = e.scrollHeight;e.dispatchEvent(new Event('scroll'));});
    await page.locator('#accept-terms').check();
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="2"]:visible', {timeout: 30000});
    await page.locator('#accept-plan').check();
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="4"]:visible', {timeout: 180000});
    const result = JSON.parse(await page.locator('#result-details').textContent());
    await page.screenshot({path: path.join(evidenceDir, action + '.png'), fullPage: true});
    if (result.status !== expectedStatus) throw new Error('Unexpected result: ' + JSON.stringify(result));
    if (!fs.readFileSync(preserved).equals(before)) throw new Error('User file bytes changed');
    const healthURL = process.env.HINSTALL_TEST_HEALTH_URL || 'http://127.0.0.1:18480/healthz';
    if (new URL(healthURL).hostname !== '127.0.0.1') throw new Error('Health URL must be loopback');
    const response = await page.request.get(healthURL);
    const health = await response.json();
    if (!response.ok() || health.ready !== true || health.product_id !== 'homensai-reference'
        || health.product_version !== expectedVersion || health.database_schema !== Number(expectedSchema))
      throw new Error('Unexpected actual product health: ' + JSON.stringify(health));
    if (errors.length) throw new Error('Browser errors: ' + JSON.stringify(errors));
    const output = {scope: 'Linux x64 frozen DEV GUI + bundled runtime', action,
      status: result.status, operation_id: result.operation_id, health, user_file_preserved: true,
      browser_errors: errors, production_ready: false};
    fs.writeFileSync(path.join(evidenceDir, 'result.json'), JSON.stringify(output, null, 2) + '\n');
    console.log(JSON.stringify(output));
  } catch (error) {
    await page.screenshot({path: path.join(evidenceDir, 'failure.png'), fullPage: true});
    console.error(JSON.stringify({error: error.message,
      wizard_error: await page.locator('#error').textContent(), browser_errors: errors}));
    process.exitCode = 1;
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error.message);process.exitCode = 1;});
