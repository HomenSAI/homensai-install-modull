/* Real browser smoke/lifecycle check against an explicitly started DEV bundle.
   No external requests, no user profile, no synthetic success substitution. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const [urlFile, evidenceDir] = process.argv.slice(2);
  if (!urlFile || !evidenceDir) throw new Error('Usage: node check_reference_gui.cjs URL_FILE EVIDENCE_DIR');
  const url = fs.readFileSync(urlFile, 'utf8').trim();
  const origin = new URL(url).origin;
  if (new URL(url).hostname !== '127.0.0.1') throw new Error('Only loopback allowed');
  fs.mkdirSync(evidenceDir, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.HINSTALL_TEST_BROWSER ? {executablePath: process.env.HINSTALL_TEST_BROWSER} : {})});
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const u = new URL(route.request().url());
    return u.hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  try {
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#product').textContent.includes('homensai-reference'));
    const locales = {};
    for (const lang of ['ru', 'en', 'de']) {
      await page.selectOption('#language', lang);
      await page.waitForFunction(lang => document.documentElement.lang === lang, lang);
      await page.screenshot({path: path.join(evidenceDir, 'environment-' + lang + '.png'), fullPage: true});
      locales[lang] = await page.locator('h1').textContent();
    }
    await page.selectOption('#language', 'ru');
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="1"]:visible', {timeout: 30000});
    await page.locator('#terms').evaluate(e => { e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event('scroll')); });
    await page.locator('#accept-terms').check();
    await page.screenshot({path: path.join(evidenceDir, 'terms-ru.png'), fullPage: true});
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="2"]:visible', {timeout: 30000});
    await page.locator('#accept-plan').check();
    await page.locator('#next').click();
    await page.waitForSelector('[data-panel="4"]:visible', {timeout: 120000});
    const result = JSON.parse(await page.locator('#result-details').textContent());
    await page.screenshot({path: path.join(evidenceDir, 'installed-ru.png'), fullPage: true});
    if (result.status !== 'SUCCEEDED') throw new Error('Installation status ' + result.status + ': ' + JSON.stringify(result.error || result.result || {}));
    if (!(await page.locator('#open-app').isVisible())) throw new Error('Ready web URL absent');
    const appURL = await page.locator('#open-app').getAttribute('href');
    const response = await page.request.get(appURL);
    if (!response.ok() || !(await response.text()).includes('HomenSAI Reference')) throw new Error('Installed product unavailable');
    const output = {scope: 'Linux x64 frozen DEV GUI + bundled runtime', locales,
      status: result.status, operation_id: result.operation_id, browser_errors: errors,
      product_ready: true, production_ready: false};
    if (process.env.HINSTALL_TEST_FULL_LIFECYCLE === '1') {
      const password = 'reference-test-only-archive-password';
      const installedRoot = await page.locator('#install-root').inputValue();
      const preservedFile = path.join(installedRoot, 'data', 'user-files', 'acceptance-preserved.txt');
      fs.mkdirSync(path.dirname(preservedFile), {recursive: true});
      fs.writeFileSync(preservedFile, 'Synthetic user data must survive uninstall and restore.');
      const actions = [];
      async function action(name, language, archivePath) {
        await page.locator('#maintenance').click();
        await page.selectOption('#language', language);
        await page.waitForFunction(language => document.documentElement.lang === language, language);
        await page.selectOption('#action', name);
        if (['backup', 'restore', 'update', 'repair', 'rollback'].includes(name))
          await page.locator('#archive-password').fill(password);
        if (archivePath) await page.locator('#archive-path').fill(archivePath);
        if (['restore', 'rollback'].includes(name)) await page.locator('#data-loss').check();
        await page.locator('#next').click();
        await page.waitForSelector('[data-panel="1"]:visible', {timeout: 30000});
        await page.locator('#terms').evaluate(e => {e.scrollTop = e.scrollHeight;e.dispatchEvent(new Event('scroll'));});
        await page.locator('#accept-terms').check();
        await page.locator('#next').click();
        await page.waitForSelector('[data-panel="2"]:visible', {timeout: 30000});
        await page.locator('#accept-plan').check();
        await page.locator('#next').click();
        await page.waitForSelector('[data-panel="4"]:visible', {timeout: 120000});
        const value = JSON.parse(await page.locator('#result-details').textContent());
        if (value.status !== 'SUCCEEDED') throw new Error(name + ': ' + JSON.stringify(value.error || value.result || {}));
        actions.push({action: name, locale: language, status: value.status, operation_id: value.operation_id});
        await page.screenshot({path: path.join(evidenceDir, name + '-' + language + '.png'), fullPage: true});
        return value.result;
      }
      const saved = await action('backup', 'en');
      if (!saved.archive?.path) throw new Error('Backup did not return archive path');
      await action('uninstall', 'de');
      if (!fs.readFileSync(preservedFile, 'utf8').includes('must survive')) throw new Error('Uninstall removed user file');
      await action('restore', 'ru', saved.archive.path);
      if (!fs.readFileSync(preservedFile, 'utf8').includes('must survive')) throw new Error('Restore lost user file');
      const ready = await page.request.get(appURL);
      if (!ready.ok() || !(await ready.text()).includes('HomenSAI Reference')) throw new Error('Restored product unavailable');
      await page.locator('#support > summary').click();
      await page.locator('#statistics').click();
      await page.waitForFunction(() => document.querySelector('#statistics-results').textContent.length > 5);
      await page.locator('#error-query').fill('INTEGRITY_FAILED');
      await page.locator('#search-errors').click();
      await page.waitForFunction(() => document.querySelector('#catalog-results').textContent.includes('INTEGRITY_FAILED'));
      await page.locator('#preview-export').click();
      await page.waitForFunction(() => document.querySelector('#export-preview').textContent.includes('operations'));
      const preview = await page.locator('#export-preview').textContent();
      if (preview.includes(password) || preview.includes('hinstall-token')) throw new Error('Secret leaked to export');
      await page.locator('#confirm-export').check();
      const downloadPromise = page.waitForEvent('download');
      await page.locator('#export').click();
      const download = await downloadPromise;
      await download.saveAs(path.join(evidenceDir, 'diagnostic-export.json'));
      await action('stop', 'en');
      output.lifecycle = actions;
      output.support_export = 'PASS';
    }
    fs.writeFileSync(path.join(evidenceDir, 'result.json'), JSON.stringify(output, null, 2));
    console.log(JSON.stringify(output));
    if (errors.length) throw new Error('Browser console errors');
  } catch (error) {
    await page.screenshot({path: path.join(evidenceDir, 'failure.png'), fullPage: true});
    const safe = await page.locator('#error').textContent();
    console.error(JSON.stringify({error: error.message, wizard_error: safe, browser_errors: errors}));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error.message);process.exitCode = 1;});
