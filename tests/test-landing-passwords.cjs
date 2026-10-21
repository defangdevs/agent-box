const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '../docs/index.html'), 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'landing page has an inline launch script');

async function loadPage({ cryptoAvailable = true, hashAvailable = true, hashFails = false } = {}) {
  const elements = new Map();
  for (const cloud of ['aws', 'azure']) {
    for (const suffix of ['password-manual', 'password-generated', 'password-generated-value', 'password-copy']) {
      elements.set(`${cloud}-${suffix}`, {
        hidden: suffix === 'password-generated',
        textContent: '',
        dataset: {},
        addEventListener(_name, listener) { this.click = listener; },
      });
    }
  }

  const links = [{ href: 'https://console.aws.amazon.com/first' }, { href: 'https://console.aws.amazon.com/second' }];
  const clipboard = [];
  const hashInputs = [];
  let nextByte = 0;
  const context = {
    document: {
      getElementById: id => elements.get(id),
      querySelectorAll: selector => {
        assert.equal(selector, '[data-region-launch]');
        return links;
      },
    },
    crypto: cryptoAvailable ? {
      getRandomValues(bytes) {
        for (let i = 0; i < bytes.length; i++) bytes[i] = nextByte++ & 255;
        return bytes;
      },
    } : undefined,
    hashwasm: hashAvailable ? {
      argon2id(options) {
        hashInputs.push(options);
        return hashFails ? Promise.reject(new Error('hash failed')) : Promise.resolve('encoded-argon2id-hash');
      },
    } : undefined,
    navigator: { clipboard: { writeText(value) { clipboard.push(value); return Promise.resolve(); } } },
    btoa: binary => Buffer.from(binary, 'binary').toString('base64'),
    Uint8Array,
    encodeURIComponent,
    setTimeout() {},
    console: { error() {} },
  };
  context.window = context;
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return { elements, links, clipboard, hashInputs };
}

async function main() {
  const page = await loadPage();
  const element = id => page.elements.get(id);
  const awsPassword = element('aws-password-generated-value').textContent;
  const azurePassword = element('azure-password-generated-value').textContent;

  assert.equal(awsPassword.length, 32);
  assert.equal(azurePassword.length, 32);
  assert.notEqual(awsPassword, azurePassword, 'separate deployments get separate passwords');
  for (const cloud of ['aws', 'azure']) {
    assert.equal(element(`${cloud}-password-manual`).hidden, true);
    assert.equal(element(`${cloud}-password-generated`).hidden, false);
    element(`${cloud}-password-copy`).click();
  }
  assert.deepEqual(page.clipboard, [awsPassword, azurePassword]);
  assert.equal(page.hashInputs.length, 1);
  assert.equal(page.hashInputs[0].password, awsPassword);
  for (const link of page.links) {
    assert.match(link.href, /param_WebPasswordHash=encoded-argon2id-hash/);
    assert.ok(!link.href.includes(awsPassword));
    assert.ok(!link.href.includes(azurePassword));
  }
  const azureLink = html.match(/class="launch azure" href="([^"]+)"/)?.[1];
  assert.ok(azureLink);
  assert.ok(!azureLink.includes('webPassword'));

  const noHash = await loadPage({ hashAvailable: false });
  assert.equal(noHash.elements.get('azure-password-generated').hidden, false);
  assert.equal(noHash.elements.get('aws-password-manual').hidden, false);
  assert.ok(noHash.links.every(link => !link.href.includes('param_WebPasswordHash')));

  const failedHash = await loadPage({ hashFails: true });
  assert.equal(failedHash.elements.get('azure-password-generated').hidden, false);
  assert.equal(failedHash.elements.get('aws-password-manual').hidden, false);

  const noCrypto = await loadPage({ cryptoAvailable: false });
  for (const cloud of ['aws', 'azure']) {
    assert.equal(noCrypto.elements.get(`${cloud}-password-generated`).hidden, true);
    assert.equal(noCrypto.elements.get(`${cloud}-password-manual`).hidden, false);
  }
  console.log('landing password generation and fallbacks: OK');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
