// Issue #25: generate the browser terminal password HERE, in the visitor's
// own browser, and hand AWS only its Argon2id hash - never the plaintext.
// Azure gets a separate locally generated password that the visitor pastes
// into its secure deployment field. AWS hashing failure does not prevent
// Azure generation; either manual note remains visible without browser crypto.
(function () {
  "use strict";

  function randomPassword() {
    // 24 random bytes -> 32 base64url characters, no padding: 192
    // bits of entropy, and every character is already safe to paste into a
    // URL, a shell, or CloudFormation's console form.
    var bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function hashPassword(password) {
    var salt = new Uint8Array(16);
    crypto.getRandomValues(salt);
    // Caddy's own `hash-password --algorithm argon2id` defaults to
    // m=47104,t=1,p=1; these cost parameters are deliberately higher; a
    // browser has to eat this cost exactly once per visit, and the module's
    // agent-web-auth-secrets.service reads whatever parameters are already
    // encoded in the hash string, so there is no need to match its default.
    return hashwasm.argon2id({
      password: password,
      salt: salt,
      parallelism: 1,
      iterations: 3,
      memorySize: 65536,
      hashLength: 32,
      outputType: "encoded",
    });
  }

  function showGeneratedPassword(cloud, password) {
    document.getElementById(cloud + "-password-generated-value").textContent = password;
    document.getElementById(cloud + "-password-manual").hidden = true;
    document.getElementById(cloud + "-password-generated").hidden = false;
  }

  function fillLaunchButtons(hash) {
    var param = "&param_WebPasswordHash=" + encodeURIComponent(hash);
    var links = document.querySelectorAll("[data-region-launch]");
    for (var i = 0; i < links.length; i++) {
      links[i].href = links[i].href + param;
    }
  }

  function wireCopyButton(cloud, password) {
    var btn = document.getElementById(cloud + "-password-copy");
    btn.addEventListener("click", function () {
      if (!navigator.clipboard || !navigator.clipboard.writeText) return;
      navigator.clipboard.writeText(password).then(function () {
        btn.textContent = "Copied";
        btn.dataset.copied = "true";
        setTimeout(function () {
          btn.textContent = "Copy";
          delete btn.dataset.copied;
        }, 2000);
      }, function () {
        // Clipboard permission denied or unavailable (e.g. non-HTTPS,
        // very old browser) - the value is still selectable text in the
        // page, so there is nothing more to do here.
      });
    });
  }

  if (!window.crypto || !crypto.getRandomValues) {
    return; // no browser crypto - both manual notes stay up.
  }

  var azurePassword = randomPassword();
  showGeneratedPassword("azure", azurePassword);
  wireCopyButton("azure", azurePassword);

  if (!window.hashwasm) return; // Azure still works if AWS hashing is unavailable.

  var awsPassword = randomPassword();
  hashPassword(awsPassword).then(function (hash) {
    fillLaunchButtons(hash);
    showGeneratedPassword("aws", awsPassword);
    wireCopyButton("aws", awsPassword);
  }, function (err) {
    // Hashing itself failed (e.g. a browser with no WebAssembly) - leave
    // the launch buttons and the manual note exactly as rendered.
    window.console && console.error("agent-box: could not hash the generated password", err);
  });
})();
