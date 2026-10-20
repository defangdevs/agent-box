// Browser coverage for the temporary resumable receiver (issue #898).
//
// The link is minted on the target box, so this test is opt-in beside the
// ordinary authenticated e2e suite:
//
//   E2E_BASE_URL=https://box E2E_RECEIVE_URL='https://box/user/receive/.../#...'
//     playwright test -c tests/e2e tests/e2e/receive.spec.ts

import { test, expect } from '@playwright/test';

const receiveUrl = process.env.E2E_RECEIVE_URL;

test('fragment capability drives a resumable upload', async ({ page }) => {
  test.skip(!receiveUrl, 'E2E_RECEIVE_URL is not configured');

  const token = new URL(receiveUrl!).hash.slice(1);
  const apiRequests: Array<{ method: string; auth?: string; url: string }> = [];
  let interrupted = false;

  await page.route('**/files**', async route => {
    const request = route.request();
    apiRequests.push({
      method: request.method(),
      auth: request.headers()['authorization'],
      url: request.url(),
    });
    // Prove the page's retry path in a real browser. The protocol suite has
    // the stronger partial-body case; aborting here checks that fetch failure
    // leads to HEAD reconciliation and continuation rather than a restart.
    if (!interrupted && request.method() === 'PATCH') {
      interrupted = true;
      await route.abort('connectionreset');
    } else {
      await route.continue();
    }
  });

  const navigation = await page.goto(receiveUrl!);
  expect(navigation?.status()).toBe(200);
  expect(navigation?.url()).not.toContain(token);
  await page.locator('#file').setInputFiles({
    name: 'browser-resume.bin',
    mimeType: 'application/octet-stream',
    // Larger than the page's 8 MiB chunk, so a successful run necessarily
    // exercises more than one PATCH after the injected interruption.
    buffer: Buffer.alloc(10 << 20, 0x5a),
  });
  await page.locator('#send').click();
  await expect(page.locator('#status')).toHaveText(
    'Upload complete. You may close this page.', { timeout: 30_000 });

  expect(interrupted).toBe(true);
  expect(apiRequests.filter(request => request.method === 'PATCH').length)
    .toBeGreaterThanOrEqual(3);
  for (const request of apiRequests) {
    expect(request.auth).toBe('Bearer ' + token);
    expect(request.url).not.toContain(token);
    expect(request.url).not.toContain('#');
  }
});
