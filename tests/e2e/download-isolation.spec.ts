// Browser e2e for the ~/downloads origin isolation (issue #631), driving a
// real Chromium against a live instance. Complements tests/web-surface.nix
// (which curls the response headers inside a VM) by covering the one thing
// only a browser can settle: whether Chromium, holding the operator's
// ambient __Host- auth cookie, actually refuses to execute a hostile
// artifact served from the management origin.
//
// The artifact is the realistic one: an agent drops a generated report into
// ~/downloads, the report came from somewhere untrusted, and the operator
// clicks the link. Served inline it would be same-origin privileged
// JavaScript -- HttpOnly stops it READING the auth cookie, not sending it,
// and SameSite and the settings daemon's CSRF guard both see a perfectly
// ordinary same-origin request.
//
// Needs E2E_DOWNLOADS_DIR, so it only runs where the runner shares a
// filesystem with the box (the box itself, or a rig with the drop directory
// mounted). See playwright.config.ts for the rest of the E2E_* contract.

import { test, expect, Browser, Page } from '@playwright/test';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';

const USER = process.env.E2E_USER || 'claude';
const PASSWORD = process.env.E2E_PASSWORD || '';
const DROP = process.env.E2E_DOWNLOADS_DIR || '';

const HOSTILE_HTML = `<!doctype html><title>quarterly report</title>
<h1>quarterly report</h1>
<script>
  window.__ARTIFACT_RAN__ = true;
  fetch('/${USER}/settings', { credentials: 'include' });
</script>`;

// SVG is the same hole with a different content type: opened as a top-level
// document (not as an <img>) its <script> runs like any other document's.
const HOSTILE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <text x="4" y="20">chart</text>
  <script type="application/ecmascript"><![CDATA[
    window.__ARTIFACT_RAN__ = true;
    fetch('/${USER}/settings', { credentials: 'include' });
  ]]></script>
</svg>`;

test.describe('~/downloads origin isolation', () => {
  test.skip(!DROP,
    'E2E_DOWNLOADS_DIR is required: the runner must share the box filesystem');

  // A directory of our own inside the drop, so a failed run cannot leave
  // artifacts sitting in the operator's file list under plausible names.
  let dir = '';
  const urlFor = (name: string) => `/${USER}/downloads/${basename(dir)}/${name}`;

  test.beforeAll(() => {
    if (!process.env.E2E_BASE_URL) throw new Error('E2E_BASE_URL is required');
    if (!PASSWORD) throw new Error('E2E_PASSWORD is required');
    dir = mkdtempSync(join(DROP, `e2e-631-${Date.now()}-`));
    writeFileSync(join(dir, 'report.html'), HOSTILE_HTML);
    writeFileSync(join(dir, 'chart.svg'), HOSTILE_SVG);
    writeFileSync(join(dir, 'index.html'), '<h1>ATTACKER INDEX</h1>');
  });

  test.afterAll(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  // The credential the attack needs: a signed-in browser context, exactly
  // what an operator reading their box from a phone has.
  async function signedInPage(browser: Browser): Promise<Page> {
    const ctx = await browser.newContext({
      httpCredentials: { username: USER, password: PASSWORD },
      acceptDownloads: true,
    });
    const page = await ctx.newPage();
    await page.goto(`/${USER}/`);
    return page;
  }

  for (const [what, file] of [['HTML', 'report.html'], ['SVG', 'chart.svg']] as const) {
    test(`a hostile ${what} artifact downloads instead of running`, async ({ browser }) => {
      const page = await signedInPage(browser);

      // Anything the artifact's script would do is a request from this page.
      const managementRequests: string[] = [];
      page.on('request', (r) => {
        if (r.url().includes(`/${USER}/settings`)) managementRequests.push(r.url());
      });

      const download = page.waitForEvent('download', { timeout: 15000 });
      // Navigating to an attachment aborts the navigation, which is the
      // point: the browser saves the file rather than making a document of it.
      await page.goto(urlFor(file)).catch(() => { /* net::ERR_ABORTED */ });
      await expect(download).resolves.toBeTruthy();

      // The page never left the workspace, so nothing of the artifact ran.
      expect(page.url()).not.toContain(file);
      expect(await page.evaluate(() => '__ARTIFACT_RAN__' in window)).toBe(false);
      expect(managementRequests).toEqual([]);

      await page.context().close();
    });

    test(`the ${what} artifact is served as an attachment under a sandbox CSP`,
      async ({ request }) => {
        const res = await request.get(urlFor(file));
        expect(res.status()).toBe(200);
        expect(res.headers()['content-disposition']).toBe('attachment');
        expect(res.headers()['content-security-policy'])
          .toBe("sandbox; frame-ancestors 'none'");
        expect(res.headers()['x-content-type-options']).toBe('nosniff');
      });
  }

  test('the listing is still Caddy\'s own page, not a dropped index.html',
    async ({ request }) => {
      const res = await request.get(`/${USER}/downloads/${basename(dir)}/`);
      expect(res.status()).toBe(200);
      // A listing has to render, so it is the one response with no attachment
      // disposition -- which is exactly why an index.html must not be able to
      // take its place.
      expect(res.headers()['content-disposition']).toBeUndefined();
      const body = await res.text();
      expect(body).not.toContain('ATTACKER INDEX');
      expect(body).toContain('report.html');
    });

  test('the workspace still frames its own terminal', async ({ browser }) => {
    const page = await signedInPage(browser);
    // frame-ancestors 'self': the tabbed workspace embeds each session's
    // terminal from this same origin, and that must keep working.
    const frame = page.frameLocator('iframe.pane').first();
    await expect(frame.locator('body')).toBeAttached({ timeout: 15000 });
    await page.context().close();
  });

  test('another origin cannot frame a management page', async ({ browser }) => {
    const page = await signedInPage(browser);
    const base = process.env.E2E_BASE_URL as string;
    // An opaque (data:) origin stands in for any attacker page. Blocked, the
    // frame is left at its initial empty document; allowed, it would carry
    // the settings page's own markup.
    await page.goto(`data:text/html,<iframe src="${base}/${USER}/settings"></iframe>`);
    await page.waitForTimeout(3000);
    const framed = await page.frames()[1]?.evaluate(() => document.body?.innerHTML ?? '');
    expect(framed ?? '').toBe('');
    await page.context().close();
  });
});
