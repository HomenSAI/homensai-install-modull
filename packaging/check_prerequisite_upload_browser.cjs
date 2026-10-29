/* Настоящий Chromium: установленный wheel + signed synthetic loopback fixture.
   Проверяет передачу файла, локализацию/keyboard/zoom; OS installer не исполняется. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {chromium} = require('playwright');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

async function main() {
  const [urlFile, fixtureDir, evidenceDir] = process.argv.slice(2);
  if (!urlFile || !fixtureDir || !evidenceDir) throw Error('Usage: node check_prerequisite_upload_browser.cjs URL_FILE FIXTURE_DIR EVIDENCE_DIR');
  const url = fs.readFileSync(urlFile, 'utf8').trim(), parsed = new URL(url);
  assert.equal(parsed.hostname, '127.0.0.1'); assert.equal(parsed.protocol, 'http:');
  const origin = parsed.origin;
  fs.mkdirSync(evidenceDir, {recursive: true});
  const fixture = JSON.parse(fs.readFileSync(path.join(fixtureDir, 'fixture.json'), 'utf8'));
  const token = new URLSearchParams(parsed.hash.slice(1)).get('token');
  const started = new Date().toISOString(), cases = [], browserErrors = [], externalRequests = [];
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  let failed, currentPage;
  try {
    for (const locale of ['ru', 'en', 'de']) {
      const context = await browser.newContext({viewport: {width: 1280, height: 1000}, locale});
      const page = await context.newPage(), posts = []; currentPage = page;
      page.on('pageerror', error => browserErrors.push({locale, message: error.message}));
      page.on('request', request => {if (request.method() === 'POST') posts.push(new URL(request.url()).pathname);});
      await context.route('**/*', route => {
        const u = new URL(route.request().url());
        if (u.origin === origin) return route.continue();
        externalRequests.push(u.origin); return route.abort();
      });
      const translationsResponse = await context.request.get(origin + '/api/v1/locales/' + locale, {headers: {'X-HInstall-Token': token}});
      assert.equal(translationsResponse.status(), 200);
      const translations = (await translationsResponse.json()).result;
      await page.goto(url);
      await page.waitForFunction(() => document.querySelector('#prereq-review') && !document.querySelector('#prereq-review').disabled);
      await page.selectOption('#language', locale);
      await page.waitForFunction(locale => document.documentElement.lang === locale && !document.querySelector('#prereq-review').disabled, locale);
      async function automaticReview() {
        if (!(await page.locator('#prereq-choice-base').count())) {
          await page.locator('#prereq-review').click();
          await page.waitForSelector('#prereq-choice-base');
        }
        if (await page.locator('#prereq-choice-base').inputValue() !== 'auto') await page.selectOption('#prereq-choice-base', 'auto');
        await page.locator('#prereq-review').click();
        await page.waitForFunction(() => !document.querySelector('#prereq-review-panel').hidden && !document.querySelector('#prereq-review').disabled);
        await page.waitForSelector('#prereq-file-base-package');
      }
      await automaticReview();
      const input = () => page.locator('#prereq-file-base-package');
      const upload = () => page.locator('#prereq-upload-base-package');
      const state = () => page.locator('#prereq-upload-status-base-package');
      const cancel = () => page.locator('#prereq-upload-cancel-base-package');
      async function waitState(key) {
        await page.waitForFunction(({key, expected}) => document.querySelector('#prereq-upload-status-base-package')?.textContent === expected,
          {key, expected: translations[key]});
      }
      assert.equal(await input().getAttribute('type'), 'file');
      assert.equal(await upload().isDisabled(), true);
      assert.equal(await page.locator('#prereq-start').isDisabled(), true);
      await input().focus();
      assert.equal(await page.evaluate(() => document.activeElement.id), 'prereq-file-base-package');
      const beforeSelectionPosts = posts.length;
      await input().setInputFiles(path.join(fixtureDir, 'oversize-installer.bin'));
      await waitState('prereq_local_wrong_size');
      assert.equal(posts.length, beforeSelectionPosts);
      assert.equal(await upload().isDisabled(), true);
      cases.push({locale, scenario: 'wrong-size-no-transfer', status: 'PASS', shown: await state().textContent()});

      await input().setInputFiles(path.join(fixtureDir, 'wrong-installer.bin'));
      await waitState('prereq_local_selected_ready');
      assert.equal(posts.length, beforeSelectionPosts, 'File selection started network effects');
      await upload().click();
      await waitState('prereq_local_failed');
      await page.waitForSelector('#error:not([hidden])');
      assert.equal(await page.locator('#error-code').textContent(), 'INTEGRITY_FAILED');
      assert.equal(await page.locator('#prereq-start').isDisabled(), true);
      await page.locator('#dismiss').click();
      cases.push({locale, scenario: 'wrong-hash-server-refusal', status: 'PASS', shown: await state().textContent()});

      await input().setInputFiles(path.join(fixtureDir, 'correct-installer.bin'));
      let aborted = false;
      const contentPattern = '**/api/v1/prerequisites/uploads/*/content';
      await page.route(contentPattern, route => {aborted = true; return route.abort('connectionreset');}, {times: 1});
      await upload().click();
      await waitState('prereq_local_failed');
      assert.equal(aborted, true);
      await page.locator('#dismiss').click();
      cases.push({locale, scenario: 'network-fault-injection-no-install', status: 'PASS'});

      let heldResolve, seenResolve;
      const held = new Promise(resolve => {heldResolve = resolve;});
      const seen = new Promise(resolve => {seenResolve = resolve;});
      await page.route(contentPattern, async route => {
        seenResolve(); await held;
        try {await route.continue();} catch (error) {if (!String(error).includes('closed') && !String(error).includes('canceled')) throw error;}
      }, {times: 1});
      await upload().click(); await seen;
      assert.equal(await cancel().isDisabled(), false);
      assert.equal(await page.locator('#prereq-review').isDisabled(), true);
      await cancel().focus(); await page.keyboard.press('Enter'); heldResolve();
      await waitState('prereq_local_canceled');
      assert.equal(await page.locator('#prereq-start').isDisabled(), true);
      cases.push({locale, scenario: 'keyboard-cancel-held-request', status: 'PASS', shown: await state().textContent()});

      await upload().click();
      await waitState('prereq_local_verified');
      assert.equal(await page.locator('#prereq-review-panel').isHidden(), true);
      assert.equal(await page.locator('#prereq-start').isDisabled(), true);
      const planResponsePromise = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/prerequisites/plans');
      await page.locator('#prereq-review').click();
      const planResponse = await planResponsePromise;
      assert.equal(planResponse.status(), 200);
      const reviewed = (await planResponse.json()).result;
      assert.equal(typeof reviewed.plan.local_artifacts['base-package'], 'string');
      assert.match(reviewed.plan.local_artifacts['base-package'], /^[a-z][a-z0-9_-]{0,79}$/);
      assert.equal(JSON.stringify(reviewed).includes(fixtureDir), false);
      assert.equal(JSON.stringify(reviewed).includes('synthetic-never-public'), false);
      assert.equal(await page.locator('#prereq-start').isDisabled(), true);
      await page.waitForFunction(() => !document.querySelector('#prereq-review').disabled);
      cases.push({locale, scenario: 'verified-file-new-plan-still-consent-required', status: 'PASS', plan_digest: reviewed.plan_digest, artifact_sha256: fixture.artifact.sha256});

      await page.evaluate(() => {document.documentElement.style.zoom = '2';});
      await input().scrollIntoViewIfNeeded(); await input().focus();
      assert.equal(await page.evaluate(() => document.activeElement.id), 'prereq-file-base-package');
      const screenshot = path.join(evidenceDir, 'upload-verified-' + locale + '-200percent.png');
      await page.screenshot({path: screenshot, fullPage: true});
      cases.push({locale, scenario: '200percent-css-zoom-keyboard-focus', status: 'PASS', screenshot: path.basename(screenshot), screenshot_sha256: sha(fs.readFileSync(screenshot))});
      await page.reload();
      await page.waitForFunction(() => !document.querySelector('#prereq-review').disabled);
      assert.equal(await page.locator('#language').inputValue(), locale);
      await automaticReview();
      assert.equal(await input().inputValue(), '');
      await waitState('prereq_local_not_selected');
      assert.equal(await upload().isDisabled(), true);
      cases.push({locale, scenario: 'reload-requires-explicit-reselection', status: 'PASS'});
      const reopened = await context.newPage(); await reopened.goto(url);
      await reopened.waitForFunction(() => !document.querySelector('#prereq-review').disabled);
      assert.equal(await reopened.locator('#prereq-start').isDisabled(), true);
      assert.equal(posts.some(value => value === '/api/v1/prerequisites/operations'), false);
      assert.equal(posts.some(value => value === '/api/v1/operations'), false);
      cases.push({locale, scenario: 'reopen-no-implicit-install', status: 'PASS', post_count: posts.length});
      await context.close();
    }
    assert.deepEqual(browserErrors, []); assert.deepEqual(externalRequests, []);
    const ledger = JSON.parse(fs.readFileSync(path.join(fixtureDir, 'ledger.json'), 'utf8'));
    assert.deepEqual(ledger.installer_calls, []); assert.deepEqual(ledger.transport_calls, []); assert.deepEqual(ledger.product_calls, []); assert.deepEqual(ledger.denied, []);
  } catch (error) {failed = error; if (currentPage && !currentPage.isClosed()) try {await currentPage.screenshot({path: path.join(evidenceDir, 'failure.png'), fullPage: true});} catch {}}
  finally {await browser.close();}
  const receipt = {schema_version: 1, status: failed ? 'FAIL' : 'PASS', started_at_utc: started, finished_at_utc: new Date().toISOString(),
    scope: 'Actual headless Chromium on installed wheel, Linux x64 DEV runner; signed synthetic prerequisite; ru/en/de. Not Windows/Ubuntu clean-host/frozen/full lifecycle acceptance; container isolation is not attested.',
    task_ids: ['HIM-033', 'HIM-029', 'HIM-035', 'HIM-102', 'HIM-062', 'HIM-063'], scenario_ids: ['ACC-05', 'ACC-07', 'ACC-08', 'ACC-16', 'ACC-23'],
    ledger: JSON.parse(fs.readFileSync(path.join(fixtureDir, 'ledger.json'), 'utf8')),
    browser_version: browser.version(), harness_sha256: sha(fs.readFileSync(__filename)), fixture, cases, browser_errors: browserErrors, external_requests: externalRequests,
    not_run: ['Real OS installer', 'Full product lifecycle', 'Windows11', 'Actual browser zoom toolbar', 'Reboot/relogin', 'Production catalog/signing'],
    production_ready: false, ...(failed ? {error: {type: failed.name, message: failed.message}} : {})};
  fs.writeFileSync(path.join(evidenceDir, 'browser-upload.json'), JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify({status: receipt.status, cases: cases.length, receipt: path.join(evidenceDir, 'browser-upload.json')}));
  if (failed) throw failed;
}
main().catch(error => {console.error(error.stack); process.exitCode = 1;});
