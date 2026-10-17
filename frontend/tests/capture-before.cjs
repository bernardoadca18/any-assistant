// Render the original HEAD in a temporary directory. Never resets the user's checkout.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const tmp = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'any-assistant-before-')));
const out = path.join(root, 'evidence');
const files = execFileSync('git', ['-C', root, 'ls-tree', '-r', '--name-only', 'HEAD', 'frontend'], { encoding: 'utf8' }).trim().split('\n').filter(file => !file.includes(':'));
for (const file of files) { const dest = path.join(tmp, file.slice('frontend/'.length)); fs.mkdirSync(path.dirname(dest), { recursive: true }); fs.writeFileSync(dest, execFileSync('git', ['-C', root, 'show', `HEAD:${file}`])); }
fs.symlinkSync(path.join(root, 'frontend/node_modules'), path.join(tmp, 'node_modules'), 'junction');
const configFile = path.join(tmp, 'vite.config.ts');
fs.writeFileSync(configFile, fs.readFileSync(configFile, 'utf8').replace('server: {', `server: { fs: { allow: [${JSON.stringify(tmp)}, ${JSON.stringify(root)}] },`));
const server = spawn(process.execPath, [path.join(root, 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '3002'], { cwd: tmp, windowsHide: true, stdio: 'ignore' });
(async () => {
  let browser;
  try {
    for (let i = 0; i < 50; i++) { try { const r = await fetch('http://127.0.0.1:3002'); if (r.ok) break; } catch {} await new Promise(resolve => setTimeout(resolve, 200)); }
    browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || 'msedge' });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.route('**/v1/**', route => route.fulfill({ status: 503, json: { detail: 'Isolated visual test; real API disabled' }, headers: { 'access-control-allow-origin': '*' } }));
    await page.goto('http://127.0.0.1:3002');
    await page.getByRole('button', { name: 'Chat Workspace', exact: true }).click();
    await page.screenshot({ path: path.join(out, 'before-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(out, 'before-mobile.png') });
    console.log('PASS: before screenshots captured from HEAD; APIs blocked.');
  } finally { await browser?.close(); server.kill(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
