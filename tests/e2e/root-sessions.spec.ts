// Root workspace e2e (tabbed sessions, issue 119; auth shape from 56/59).
//
// The vhost root is the primary user's tabbed terminal workspace, served by
// the settings daemon behind the same cookie-or-basic auth as the terminal:
// a tab per session, each pane an iframe onto the per-session ttyd URL.
// Complements tests/sessions.nix (which curls the HTTP surface inside a VM)
// by covering the browser-only legs:
//   - the auth gate on / (401 unauthenticated; basic auth renders the page,
//     which the root redirects to /<user>/ — every goto('/') below follows it),
//   - the active tab's pane iframing /<user>/<session>/ with no URL
//     userinfo (issue 56: Chrome answers the basic-auth challenge with
//     userinfo plus an EMPTY password, and typed credentials can't override),
//   - client-side tab switching that keeps background panes mounted,
//   - the add-session flow from the tab bar (new tab appears and activates),
//   - dismissing the feedback banner without reloading the workspace,
//   - session restart/delete on the settings page, incl. the confirm() guard,
//   - the live feed: sessions created or deleted elsewhere show up in an
//     already-open page without a reload.
//
// See playwright.config.ts for the E2E_* environment contract.

import { test, expect, Browser, Page } from '@playwright/test';

const USER = process.env.E2E_USER || 'claude';
const PASSWORD = process.env.E2E_PASSWORD || '';

// Session names are auto-derived by the daemon (the add form has no name
// field since #210), so a test that adds one identifies it as "the tab that
// was not there before".
const tabNames = (page: Page): Promise<string[]> =>
  page.locator('#tab-bar .tab[data-tab]')
    .evaluateAll((els) => els.map((e) => e.getAttribute('data-tab') as string));

test.beforeAll(() => {
  if (!process.env.E2E_BASE_URL) throw new Error('E2E_BASE_URL is required');
  if (!PASSWORD) throw new Error('E2E_PASSWORD is required');
});

async function authedPage(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({
    httpCredentials: { username: USER, password: PASSWORD },
  });
  return ctx.newPage();
}

// The session editor collapses on load when JS is live; a toggle button
// opens it. Open it only if needed (a second click would close it again).
async function openSessionEditor(page: Page, buttonName: string) {
  if (await page.locator('#session-editor').isHidden()) {
    await page.getByRole('button', { name: buttonName }).click();
  }
}

test('root requires auth: unauthenticated request is rejected with 401', async ({ request }) => {
  const res = await request.get('/');
  expect(res.status()).toBe(401);
});

test('the old public sessions.json is gone (now behind the auth gate)', async ({ request }) => {
  const res = await request.get(`/${USER}/sessions.json`);
  expect(res.status()).toBe(401);
});

test('authenticated root shows the tab bar with main active and its terminal iframe', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');
  const mainTab = page.locator('#tab-bar .tab[data-tab="main"]');
  await expect(mainTab).toBeVisible();
  await expect(mainTab).toHaveAttribute('aria-current', 'page');
  // The pane may briefly be a "starting" placeholder; the poller swaps in
  // the iframe once the session is live.
  const frame = page.locator(`#panes iframe[src="/${USER}/main/"]`);
  await expect(frame).toBeVisible({ timeout: 30_000 });
  // Every pane records the session state it was built for, the iframe
  // included. That stamp is what lets the page retire a terminal whose
  // session has since stopped instead of leaving the dead one mounted
  // (issue 241); losing it silently brings the stale pane back.
  await expect(frame).toHaveAttribute('data-ph', 'live');
});

test('?tab= selects a tab server-side (the no-JS switching path)', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/?tab=main');
  await expect(page.locator('#tab-bar .tab[data-tab="main"]')).toHaveAttribute('aria-current', 'page');
});

test('no root-page href embeds URL userinfo (user@host)', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');
  const body = await page.content();
  const hrefs = [...body.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
  expect(hrefs.length).toBeGreaterThan(0);
  for (const href of hrefs) {
    expect(href, `userinfo in root-page link: ${href}`).not.toMatch(/^https?:\/\/[^/]*@/);
  }
});

test('add a session from the tab bar, switch tabs, delete it on the settings page', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');

  // Add: the new tab appears, becomes active, and gets a pane. The daemon
  // names the session, so take whichever tab is new.
  const before = await tabNames(page);
  await openSessionEditor(page, 'New session');
  await page.locator('#session-editor button[type="submit"]').click();
  await expect.poll(async () => (await tabNames(page)).length).toBe(before.length + 1);
  const name = (await tabNames(page)).find((n) => !before.includes(n)) as string;
  const newTab = page.locator(`#tab-bar .tab[data-tab="${name}"]`);
  await expect(newTab).toHaveAttribute('aria-current', 'page');
  await expect(page.locator(`#panes iframe[src="/${USER}/${name}/"]`))
    .toBeVisible({ timeout: 30_000 });

  // Switch back to main: its pane shows, the new pane stays mounted but
  // hidden (background sessions keep their terminal attached).
  await page.locator('#tab-bar .tab[data-tab="main"]').click();
  await expect(page.locator('#tab-bar .tab[data-tab="main"]')).toHaveAttribute('aria-current', 'page');
  await expect(newTab).not.toHaveAttribute('aria-current', 'page');
  await expect(page.locator(`#panes .pane[data-pane="${name}"]`)).toHaveCount(1);
  await expect(page.locator(`#panes .pane[data-pane="${name}"]`)).not.toBeVisible();
  await expect(page.locator(`#panes iframe[src="/${USER}/main/"]`))
    .toBeVisible({ timeout: 30_000 });

  // Delete lives on the settings page now. Dismissed confirm is a no-op;
  // accepting delists and kills the session.
  await page.goto(`/${USER}/settings/`);
  const row = page.locator('#sessions-list li', { hasText: name });
  await expect(row).toHaveCount(1);
  // By the form it posts, not by button name: the row's webhook fold (#227)
  // holds one trash per subscription, all of which answer to a "Delete"
  // accessible name.
  const del = row.locator('form[action$="/sessions/delete"] button');
  page.once('dialog', (d) => d.dismiss());
  await del.click();
  await expect(row).toHaveCount(1);
  page.once('dialog', (d) => d.accept());
  await del.click();
  await expect(page.locator('.msg')).toHaveText(/Session deleted/);
  await expect(row).toHaveCount(0);
});

// The feedback banner is dismissible (issue #246). Its x is a link back to the
// same page without the ?ok=, which is the scriptless path; the whole point of
// the client-side handler is that it does NOT navigate — a reload here would
// drop every attached terminal. Checked on the workspace, where that cost is
// real, and the tab stays put.
test('the feedback banner can be dismissed without reloading the workspace', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');
  await expect(page.locator(`#panes iframe[src="/${USER}/main/"]`))
    .toBeVisible({ timeout: 30_000 });

  const before = await tabNames(page);
  await openSessionEditor(page, 'New session');
  await page.locator('#session-editor button[type="submit"]').click();
  const msg = page.locator('.msg');
  await expect(msg).toHaveText(/Session added/);
  await expect.poll(async () => (await tabNames(page)).length).toBe(before.length + 1);
  const name = (await tabNames(page)).find((n) => !before.includes(n)) as string;

  await msg.locator('.msg-x').click();
  await expect(msg).toHaveCount(0);
  // No second navigation, so the panes were never torn down...
  expect(await page.evaluate(() => performance.getEntriesByType('navigation').length)).toBe(1);
  await expect(page.locator(`#panes .pane[data-pane="main"]`)).toHaveCount(1);
  // ...and the ?ok= is gone, so a later reload does not raise it again while
  // the selected tab survives.
  const url = new URL(page.url());
  expect(url.searchParams.get('ok')).toBeNull();
  expect(url.searchParams.get('tab')).toBe(name);

  await page.context().request.post('/sessions/delete', { form: { name } });
});

// The tab bar's x kills a live agent and sits a few pixels from the session
// name, so it arms on the first click (a red "Close?" pill) and only closes on
// the second. Covers all three legs of that guard: the first click is inert,
// the armed state gives way to a click elsewhere and to its own timeout, and
// the second click actually closes.
test('the tab close button arms first and only closes on a second click', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');

  // Add a session to close, so the test never touches "main". The name is
  // auto-derived by the daemon, so take whichever tab is new.
  const before = await tabNames(page);
  await openSessionEditor(page, 'New session');
  await page.locator('#session-editor button[type="submit"]').click();
  await expect.poll(async () => (await tabNames(page)).length).toBe(before.length + 1);
  const name = (await tabNames(page)).find((n) => !before.includes(n)) as string;

  const tab = page.locator(`#tab-bar .tab[data-tab="${name}"]`);
  const x = page.locator(`#tab-bar .tab-x[data-close="${name}"]`);
  await expect(x).toHaveText('×');

  // First click only arms: the session is still there, and the whole tab
  // tints so it is obvious which session is at stake.
  await x.click();
  await expect(x).toHaveText('Close?');
  await expect(page.locator('#tab-bar .tab-wrap.arm')).toHaveCount(1);
  await expect(tab).toHaveCount(1);

  // A click elsewhere disarms — a stray first click never stays loaded.
  await page.locator('#tab-bar .tab[data-tab="main"]').click();
  await expect(x).toHaveText('×');
  await expect(tab).toHaveCount(1);

  // A double-click is not a considered confirmation (SETTLE_MS): it arms
  // and stops there, rather than arming and immediately closing.
  await x.dblclick();
  await expect(x).toHaveText('Close?');
  await expect(tab).toHaveCount(1);

  // Nor does it stay armed indefinitely (ARM_MS in settings.js).
  await expect(x).toHaveText('×', { timeout: 10_000 });
  await expect(tab).toHaveCount(1);

  // Second click while armed: the tab and its pane go away.
  await x.click();
  await expect(x).toHaveText('Close?');
  await page.waitForTimeout(500);   // past SETTLE_MS, well inside ARM_MS
  await x.click();
  await expect(page.locator('.msg')).toHaveText(/Session deleted/);
  await expect(tab).toHaveCount(0);
  await expect(page.locator(`#panes .pane[data-pane="${name}"]`)).toHaveCount(0);
});

test('the working-directory picker suggests folders one level at a time', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');
  await openSessionEditor(page, 'New session');

  // The field defaults to the home directory.
  const cwd = page.locator('#session-editor input[name="cwd"]');
  await expect(cwd).toHaveValue('~');

  // Focusing it lists home's folders; every option is a directory (trailing /).
  await cwd.click();
  const list = page.locator('#session-editor .ac');
  await expect(list).toBeVisible();
  const first = list.locator('li[data-name]').first();
  await expect(first).toBeVisible();
  expect((await first.textContent())?.endsWith('/')).toBeTruthy();

  // Picking a folder drills in: the value gains "<name>/" (the trailing slash
  // is what lets typing continue at the next level) and the list refreshes to
  // that folder's own children.
  const picked = (await first.getAttribute('data-name'))!;
  await first.click();
  await expect(cwd).toHaveValue(`~/${picked}/`);
  await expect(list).toBeVisible();

  // Escape closes the dropdown without disturbing the chosen path.
  await cwd.press('Escape');
  await expect(list).toBeHidden();
  await expect(cwd).toHaveValue(`~/${picked}/`);
});

test('settings link on the root page reaches the settings page, which manages sessions', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto('/');
  await page.locator(`#tab-bar a[href="/${USER}/settings/"]`).click();
  await expect(page.getByRole('heading', { name: `Settings for ${USER}` })).toBeVisible();
  // The session manager moved here from the old root list page (issue 119).
  await expect(page.locator('#session-editor')).toHaveCount(1);
  await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible();
});

// Sessions change from outside whichever page is open — the
// agent-box-session CLI, an agent adding a helper for itself, a second
// browser tab — and the page has to follow along rather than go stale until
// somebody reloads it. The daemon streams a fingerprint of the session state
// and the page re-fetches when it moves. Driven here through the HTTP API,
// which is what all those out-of-band writers look like to an open page.
test('a session created and deleted elsewhere appears and goes without a reload', async ({ browser }) => {
  const ctx = await browser.newContext({
    httpCredentials: { username: USER, password: PASSWORD },
  });
  const page = await ctx.newPage();
  await page.goto('/');
  await expect(page.locator('#tab-bar .tab[data-tab="main"]')).toBeVisible();

  // Created without touching the open page at all. The name is auto-derived,
  // so take it from the redirect the daemon answers with.
  const added = await ctx.request.post('/sessions/add', {
    form: { cwd: '~' },
    maxRedirects: 0,
  });
  expect(added.status()).toBe(303);
  const name = new URL(added.headers()['location'], 'http://x')
    .searchParams.get('tab') as string;
  expect(name).toBeTruthy();

  const tab = page.locator(`#tab-bar .tab[data-tab="${name}"]`);
  await expect(tab).toBeVisible({ timeout: 15_000 });

  // ...and the settings list, open in its own page, follows the same change.
  // Match the session link exactly: every row also names its agent, and the
  // auto-derived session name IS an agent name.
  const settings = await ctx.newPage();
  await settings.goto(`/${USER}/settings/`);
  const row = settings.locator('#sessions-list a.sess')
    .filter({ hasText: new RegExp(`^${name}$`) });
  await expect(row).toHaveCount(1);

  // Deleted the same way: the tab goes without a reload either.
  const removed = await ctx.request.post('/sessions/delete', {
    form: { name },
    maxRedirects: 0,
  });
  expect(removed.status()).toBe(303);
  await expect(tab).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(`#panes .pane[data-pane="${name}"]`)).toHaveCount(0);
  await expect(row).toHaveCount(0, { timeout: 15_000 });

  // None of that was a page load: the workspace patched itself in place.
  expect(await page.evaluate(() => performance.getEntriesByType('navigation').length)).toBe(1);
  await ctx.close();
});

test('settings page links to the agent-box repository', async ({ browser }) => {
  const page = await authedPage(browser);
  await page.goto(`/${USER}/settings/`);
  const repo = page.getByRole('link', { name: 'agent-box on GitHub' });
  await expect(repo).toBeVisible();
  await expect(repo).toHaveAttribute('href', 'https://github.com/defangdevs/agent-box');
});
