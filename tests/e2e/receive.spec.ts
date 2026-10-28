// Browser coverage for the temporary resumable receiver (issue #898).
//
// The link is minted on the target box, so this test is opt-in beside the
// ordinary authenticated e2e suite:
//
//   E2E_BASE_URL=https://box E2E_RECEIVE_URL='https://box/user/receive/.../#...'
//     playwright test -c tests/e2e tests/e2e/receive.spec.ts

import { test, expect } from '@playwright/test';

const receiveUrl = process.env.E2E_RECEIVE_URL;

test('a dropped file resumes and the completed fast path checks content', async ({ page }) => {
  test.skip(!receiveUrl, 'E2E_RECEIVE_URL is not configured');
  test.setTimeout(90_000);

  const token = new URL(receiveUrl!).hash.slice(1);
  const apiRequests: Array<{
    method: string;
    auth?: string;
    ifMatch?: string;
    url: string;
  }> = [];
  let interrupted = false;

  await page.route('**/files**', async route => {
    const request = route.request();
    apiRequests.push({
      method: request.method(),
      auth: request.headers()['authorization'],
      ifMatch: request.headers()['if-match'],
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
  await expect(page.getByRole('button', { name: 'Upload file', exact: true }))
    .toHaveCount(0);
  const drop = page.locator('#drop');
  const dropFile = async (
    fill: number,
    lastModified = 1_700_000_000_000,
  ) => {
    const dataTransfer = await page.evaluateHandle(({ value, modified }) => {
      const transfer = new DataTransfer();
      // Larger than the page's 8 MiB chunk, so a successful run necessarily
      // exercises more than one PATCH after the injected interruption.
      transfer.items.add(new File(
        [new Uint8Array(10 << 20).fill(value)],
        'browser-resume.bin',
        { type: 'application/octet-stream', lastModified: modified },
      ));
      return transfer;
    }, { value: fill, modified: lastModified });
    await drop.dispatchEvent('dragenter', { dataTransfer });
    await expect(drop).toHaveClass(/dragging/);
    await drop.dispatchEvent('drop', { dataTransfer });
    await dataTransfer.dispose();
    await expect(drop).not.toHaveClass(/dragging/);
    await expect(page.locator('#prompt')).toHaveText('browser-resume.bin');
  };

  // A multi-file drop must not bind the single-file link or send requests.
  const multipleFiles = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(['first'], 'first.txt'));
    transfer.items.add(new File(['second'], 'second.txt'));
    return transfer;
  });
  await drop.dispatchEvent('drop', { dataTransfer: multipleFiles });
  await multipleFiles.dispose();
  await expect(page.locator('#status')).toHaveText('Choose exactly one file.');
  expect(apiRequests).toHaveLength(0);

  await dropFile(0x5a);
  await expect(page.locator('#file')).toBeDisabled();
  // A second drop during the interrupted transfer must be ignored.
  const extraFile = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(['extra'], 'extra.txt'));
    return transfer;
  });
  await drop.dispatchEvent('drop', { dataTransfer: extraFile });
  await extraFile.dispose();
  await expect(page.locator('#prompt')).toHaveText('browser-resume.bin');
  await expect(page.locator('#status')).toHaveText(
    'Upload complete. You may close this page.', { timeout: 30_000 });

  expect(interrupted).toBe(true);
  expect(apiRequests.filter(request => request.method === 'PATCH').length)
    .toBeGreaterThanOrEqual(3);

  // Re-selecting the same bytes may take the completed HEAD fast path, but
  // only after the browser has actually read and fingerprinted the file.
  const patchCount = apiRequests.filter(
    request => request.method === 'PATCH').length;
  await page.reload();
  await page.locator('#file').setInputFiles({
    name: 'browser-resume.bin',
    mimeType: 'application/octet-stream',
    buffer: Buffer.alloc(10 << 20, 0x5a),
  });
  await expect(page.locator('#status')).toHaveText(
    'Upload complete. You may close this page.', { timeout: 30_000 });
  expect(apiRequests.filter(request => request.method === 'PATCH').length)
    .toBe(patchCount);

  // The visible metadata is deliberately identical. Changed bytes must not
  // inherit the completed state merely because name, size, and mtime match.
  await page.reload();
  await dropFile(0x5b);
  await expect(page.locator('#status')).toHaveText(
    'This link is already bound to a different file.', { timeout: 30_000 });
  expect(apiRequests.filter(request => request.method === 'PATCH').length)
    .toBe(patchCount);
  const conditionalHeads = apiRequests.filter(request => request.ifMatch);
  expect(conditionalHeads).toHaveLength(2);
  expect(conditionalHeads.every(request => request.method === 'HEAD')).toBe(true);
  expect(conditionalHeads[0].ifMatch).not.toBe(conditionalHeads[1].ifMatch);

  // Errors unlock the picker and permit an immediate retry without a reload.
  await expect(page.locator('#file')).toBeEnabled();
  await expect(drop).not.toHaveClass(/disabled/);
  await page.locator('#file').setInputFiles({
    name: 'browser-resume.bin',
    mimeType: 'application/octet-stream',
    buffer: Buffer.alloc(10 << 20, 0x5b),
  });
  await expect(page.locator('#status')).toHaveText(
    'This link is already bound to a different file.', { timeout: 30_000 });
  await expect(page.locator('#file')).toBeEnabled();
  await expect(page.locator('#file')).toHaveValue('');
  await page.locator('#file').setInputFiles({
    name: 'browser-resume.bin',
    mimeType: 'application/octet-stream',
    buffer: Buffer.alloc(10 << 20, 0x5a),
  });
  await expect(page.locator('#status')).toHaveText(
    'Upload complete. You may close this page.', { timeout: 30_000 });
  expect(apiRequests.filter(request => request.method === 'PATCH').length)
    .toBe(patchCount);

  for (const request of apiRequests) {
    expect(request.auth).toBe('Bearer ' + token);
    expect(request.url).not.toContain(token);
    expect(request.url).not.toContain('#');
  }
});
