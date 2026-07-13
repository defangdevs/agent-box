// Root picker regression tests (issue 56).
//
// The picker (and the CFN WebURL output) must never embed URL userinfo
// (https://user@host/...): Chrome answers the basic-auth challenge with the
// userinfo username plus an EMPTY password, and credentials typed into the
// prompt cannot override the URL-embedded identity — locking users out of
// the terminal with the "correct password".

import { test, expect } from '@playwright/test';

const USER = process.env.E2E_USER || 'claude';

test('root picker serves unauthenticated and links the terminal', async ({ request }) => {
  const res = await request.get('/');
  expect(res.status()).toBe(200);
  const body = await res.text();
  expect(body).toContain(`href="https://`);
  expect(body).toContain(`/${USER}/"`);
});

test('no picker href embeds URL userinfo (user@host)', async ({ request }) => {
  const body = await (await request.get('/')).text();
  const hrefs = [...body.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
  expect(hrefs.length).toBeGreaterThan(0);
  for (const href of hrefs) {
    expect(href, `userinfo in picker link: ${href}`).not.toMatch(/^https?:\/\/[^/]*@/);
  }
});
