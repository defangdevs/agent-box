// The settings viewer consumes the same resource API as portal clients.
import { test, expect } from '@playwright/test';

const USER = process.env.E2E_USER || 'claude';
const SETTINGS_PATH = `/${USER}/settings/`;
const GiB = 1024 ** 3;
const sample = {
  ok: true,
  cpu: { busy_percent: 12.5 },
  memory: { available_bytes: 3 * GiB, total_bytes: 4 * GiB },
  disk: { available_bytes: 40 * GiB, total_bytes: 60 * GiB },
  sessions: { used: 0, limit: 4 },
};

test.use({
  httpCredentials: { username: USER, password: process.env.E2E_PASSWORD || '' },
});

test('resource readings refresh independently and fit on a phone', async ({ page }) => {
  await page.clock.install();
  await page.setViewportSize({ width: 390, height: 844 });
  let reading: object = { ...sample, cpu: null };
  await page.route(`**${SETTINGS_PATH}resources`, (route) => route.fulfill({ json: reading }));
  await page.goto(SETTINGS_PATH);
  const value = (name: string) => page.locator(`[data-resource="${name}"]`);
  await expect(value('cpu')).toHaveText('Unavailable');
  await expect(value('memory')).toHaveText('1.0 / 4.0 GiB used');
  await expect(value('disk')).toHaveText('20.0 / 60.0 GiB used');
  await expect(value('sessions')).toHaveText('0 / 4 in use');
  const grid = page.locator('.resource-grid');
  expect(await grid.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);

  reading = { ...sample, cpu: { busy_percent: 0 }, memory: null,
    sessions: { used: 5, limit: 4 } };
  await page.clock.runFor(5000);
  await expect(value('cpu')).toHaveText('0% busy');
  await expect(value('memory')).toHaveText('Unavailable');
  await expect(value('disk')).toHaveText('20.0 / 60.0 GiB used');
  await expect(value('sessions')).toHaveText('5 / 4 in use');
});

test('failed requests clear stale readings and recover on the next refresh', async ({ page }) => {
  await page.clock.install();
  let failed = false;
  await page.route(`**${SETTINGS_PATH}resources`, (route) => route.fulfill(
    failed ? { status: 503, body: 'Unavailable' } : { json: sample },
  ));
  await page.goto(SETTINGS_PATH);
  await expect(page.locator('[data-resource="cpu"]')).toHaveText('12.5% busy');
  failed = true;
  await page.clock.runFor(5000);
  await expect(page.locator('[data-resource]')).toHaveText(Array(4).fill('Unavailable'));
  failed = false;
  await page.clock.runFor(5000);
  await expect(page.locator('[data-resource="cpu"]')).toHaveText('12.5% busy');
});
