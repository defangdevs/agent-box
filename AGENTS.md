# Repository Guidelines

## Documentation Map

Read the [wiki](https://github.com/defangdevs/agent-box/wiki) before you design anything, and add to it when you settle a question that outlives your change. It carries the durable docs that this file does not:

- **[Users-vs-Sessions](https://github.com/defangdevs/agent-box/wiki/Users-vs-Sessions)** — the rule for adding a Linux user versus a session, and the proof that a session cannot be a security boundary (a sibling session's `/proc/<pid>/environ` and the shared tmux server). Any feature that implies per-session isolation is a design error; check this page first.
- **[Development](https://github.com/defangdevs/agent-box/wiki/Development)** — CI behavior: which paths trigger it, how the `docs/` landing page publishes, and the generated-module drift check.

This file owns repo conventions; the wiki owns design rationale and maintainer notes. Host-specific setup belongs in neither — `PATH`, git identity, token paths and one box's migration history describe a deployment, so they go in that host's own config repo.

## Which agent file a note belongs in

Two different files in this checkout carry instructions for agents, and they
have opposite audiences. Putting a note in the wrong one either ships repo
trivia to every deployment or buries box knowledge where only a contributor
sees it.

- **`AGENTS.md`** — this file, read by an agent working ON agent-box. The
  generated module, the checks, the commit conventions, everything below.
  `CLAUDE.md` is a committed symlink to it, because claude-code discovers
  only that name (issue #305); keep the symlink rather than a second copy.
- **`modules/src/default-agents.md`** (plus `default-agents-webhook.md` for
  the webhook section) — the guide agent-box SHIPS. It is assembled into
  `modules/agent-box.nix` and published read-only on every deployed box at
  `/etc/agent-box-guides/AGENTS.<user>.md`, describing the box an agent is
  living in: sessions, secrets, ~/downloads, ~/sites, webhooks, self-update.
  Nothing about developing agent-box belongs there, and it must stay
  deployment-independent — per-deployment additions come from
  `users.<name>.agentsMd` (e.g. `aws/template.yaml`'s `AgentsMd` parameter).

"How do I regenerate the module?" belongs here; "where do I put a file for
the user to download?" belongs in the shipped guide; "this box updated on
Tuesday" belongs in neither. A box that also has a
clone of this repo ends up with both, discovered at different scopes: the
shipped guide through the pointer seeded in `$HOME` (and, for claude,
`~/.claude/CLAUDE.md` -> the `/etc` guide), this file through the checkout it
sits in — which, since #242, every NixOS box has by default
(`selfUpdate.checkout`), so "both, at different scopes" is now the
ordinary case rather than the one our own box happens to be in. Editing the shipped guide is a module change like any other — run
`nix run .#assemble`, and the golden fixture moves with it.

A third scope sits outside this checkout and is easy to lose work in: an
agent's own editable `~/AGENTS.md` and whatever private notes its harness
keeps. Those are the right home for one box's history and for how an agent
talks to one person, and the wrong home for anything a contributor to this repo
would need - a CI gotcha, a local rig, a check's blind spot. When a note in
there turns out to be a repo convention, move it here rather than letting each
box rediscover it.

## Project Structure & Module Organization

`modules/agent-box.nix` is the portable NixOS module and the repository's main implementation. It is **generated** — do not edit it by hand. The sources are `modules/agent-box.nix.in` (the Nix template) plus the assets under `modules/src/` (e.g. the settings daemon), stitched together by `bin/assemble-module.py` via `@@include:...@@` markers. `flake.nix` exposes the module, VM image, and CI checks. Host examples live in `hosts/`; AWS deployment configuration and operational notes are in `aws/`, and the Azure equivalent in `azure/` (Bicep source plus its compiled ARM JSON, which is a build artifact - see `azure/README.md`). Put NixOS integration tests in `tests/*.nix`, live browser tests in `tests/e2e/*.spec.ts`, maintenance utilities in `scripts/`, and website images or static content in `docs/`.

Keep the module self-contained: deployed boxes fetch `modules/agent-box.nix` as a single file, so it must not import sibling files. This is why the sources are re-embedded at build time rather than loaded with `readFile` — after editing the template or `modules/src/`, run `nix run .#assemble` and commit the regenerated `modules/agent-box.nix` (CI's `module-generated-up-to-date` check fails on drift).

## Build, Test, and Development Commands

- `nix run .#assemble` regenerates `modules/agent-box.nix` from `modules/agent-box.nix.in` + `modules/src/` (run from the repo root after editing either).
- `nix build -L .#checks.<system>.module-generated-up-to-date` verifies the committed module matches its sources (`<system>` is your host's, e.g. `aarch64-linux`).
- `nix build -L .#checks.<system>.assemble-module-escaping` runs `tests/test-assemble-module.py`, the unit tests for the assembler's Nix escaping (issue #244). The up-to-date check above cannot catch an escaping bug — it regenerates the file with the same assembler, so the check and the bug agree on the wrong bytes. Runnable without Nix too: `python3 tests/test-assemble-module.py`, or `--nix` to round-trip the corpus through `nix-instantiate` instead of the lexer model.
- `nix build -L .#checks.<system>.backend-parity` compares what the NixOS module and the native renderer supply to the SAME shared payloads under `modules/src/` — every `AGENT_BOX_*` variable and every rendered service — and fails on a difference not declared in `scripts/check_backend_parity.py`. Runnable without Nix too: `python3 scripts/check_backend_parity.py`. Its two tables are the point: `BY_DESIGN` for a difference that is correct (with the reason), `KNOWN_GAPS` for one that is a bug (with its issue). Both are staleness-checked, so a gap you fix must be deleted from the table in the same change. Issue #392 — the settings page's Connections section, missing from every native box because only the module set `AGENT_BOX_CONNECT_BINS` — is the failure this exists to catch; neither backend's fixture could see it, because each ratifies whatever its own renderer emits. What it cannot see is a difference one level DOWN from a name it compares. `programs.*` supplies no `AGENT_BOX_*` variable and no unit of its own, so `programs.tmux` (#495) and `programs.git` (#498) both slipped past; and a generated wrapper belongs to no unit, so its env is a prologue the renderer writes above the `exec` - native's `agent-box-webhook-spawn` was missing three variables the check counted as supplied box-wide, because other CLIs exported the same names (PR #500 added a fourth comparison keyed by `(wrapper, variable)` for exactly that). When a native box misbehaves where a NixOS one does not, ask WHICH generated file supplies the value, not whether the name exists somewhere.
- `nix build -L .#checks.<system>.vendor-integrity` verifies every third-party file under `modules/src/vendor/` still hashes to the pin recorded in `modules/src/vendor/vendor.json`. Runnable without Nix too: `python3 scripts/check_vendor.py`. Add `--upstream` (needs network, so it is not in the Nix check) to ask each forge whether a newer release exists — `.github/workflows/vendor-updates.yml` does that weekly and files an issue.
- `nix build -L .#checks.<system>.checkout-bootstrap` runs `tests/test-checkout-bootstrap.sh` against `modules/src/checkout-cli.sh`, the script that puts this repo ON a deployed box (issue #242). Its assertions are mostly REFUSALS, because that is where the damage would be: it runs unattended at every supervisor start, in a tree sibling sessions are working in, so a realign that moved somebody's branch pointer would destroy work at boot on a box nobody is watching. `origin` is a local repository and `gh` is a shim, so there is no network and it runs natively on every architecture. Runnable without Nix too: `bash tests/test-checkout-bootstrap.sh modules/src/checkout-cli.sh`.
- `nix build -L .#checks.<system>.source-tree` runs `tests/test-source-tree.sh` against `modules/src/source-tree.sh`, the tree the box is BUILT from and the whole of what an update moves (issue #242). Weighted at the refusals for the same reason: it runs as root, unattended, and the tree it leaves behind is what the next rebuild builds — so a rewritten history, a downgrade, and a baseline the tree has never heard of each get an assertion, as does the realign that makes the fast-forward guard measure ancestry from the rev the box is RUNNING. It also pins the two locks the trust boundary rests on: `check` answers from `git ls-remote` and so creates no tree and fetches into none, and git runs with `core.hooksPath` pointed at nothing, so a `post-checkout`/`post-merge` hook in the tree cannot run as root (that assertion has a negative control — remove the lock and it fails). `origin` is a local repository, so there is no network and it runs natively on every architecture. Runnable without Nix too: `bash tests/test-source-tree.sh modules/src/source-tree.sh`.
- `nix build -L .#checks.<system>.checkout-options` is the eval regression for `selfUpdate`'s three path assertions (issue #242). It reads `config.assertions` rather than forcing `toplevel`, so a failure names WHICH assertion fired instead of only reporting that something did — and it asserts the accepting cases too, so an assertion that rejects everything fails it as loudly as one that rejects nothing. `selfUpdate.srcDir` is the one that matters most, because root BUILDS the box from that tree: it is confined to a normalized path under `/var/lib`, since owning the directory is not enough — a writable ancestor (`/home/agent/src`, `/tmp/src`) lets an agent swap the whole tree and choose what root builds. For `selfUpdate.checkout.path` the inputs that matter are the ones a first pass at "must be relative" lets through: `../agent-box` escapes the home, `.` and `""` collapse to `/home/<maintainer>` itself — which the agent unit's `ProtectSystem=strict` would refuse as EROFS inside a background job's journal — and `a//b`, which resolves to a perfectly ordinary child path and is refused for a different reason: every empty component is, because one is how the collapsing cases are spelled.
- `nix flake metadata` validates flake inputs and basic evaluation.
- `nix build .#packages.x86_64-linux.vm` builds the bootable qcow2 image under `result/`.
- `nix build -L .#checks.<system>.multi-user` runs the quick module/configuration assertion.
- `nix build -L .#checks.<system>.one-spec-both-backends` proves the two backends were asked for the SAME box, and then compares what they made of it in the places `backend-parity` cannot see — the seed JSON, the tmpfiles rules and the sudoers grants, which carry no `AGENT_BOX_*` name and are where #356's twin-schema bugs lived. `tests/native/config.json` is GENERATED from the golden web configuration's own options by `tests/spec.nix`; after a module option change that the native schema should carry, run `nix run .#update-native-config` and then `python3 tests/test_agentbox.py --update`, and commit both diffs. Runnable without Nix too (`python3 scripts/check_one_spec.py`): the module-evaluation half needs Nix and is skipped, and so is the `config.yaml` dialect check on a box without PyYAML — everything skipped is named, and the run closes with `PARTIAL`, never `OK`. Under `--spec` (the flake check, whose derivation carries pyyaml) a missing PyYAML is a failure instead: there it means the dependency was dropped, so the check is broken rather than partial. Same two-table convention as `backend-parity`, staleness-checked the same way.
- `nix build -L .#checks.<system>.golden-snapshot` verifies the rendered configuration (every generated unit, /etc file, tmpfiles rule and script payload, store hashes normalized) still byte-matches the committed `tests/golden/` fixture — the behavior lock for the issue #154 portability refactor. After an INTENTIONAL behavior change (or a flake.lock bump, which changes store path names), run `nix run .#update-golden` and commit the reviewed `tests/golden/` diff together with the change.
- `nix build -L .#checks.<system>.module-single-file` verifies standalone module evaluation.
- `nix build -L .#checks.x86_64-linux.<name>` runs an individual VM test such as `sessions` or `settings-page`.

The eval-level checks (`multi-user`, `module-single-file`, `download-route`, `webhook-route`, `module-generated-up-to-date`, `assemble-module-escaping`) and `nix run .#assemble` are exposed for both `x86_64-linux` and `aarch64-linux`, so they run natively on a Graviton box like the deployed fleet. The qcow2 image and the interactive `runNixOSTest` checks are `x86_64-linux`-only: a NixOS test needs a same-arch KVM guest, so building them from another arch would fall back to unusably slow TCG. Widen `vmSystems` in `flake.nix` to offer them elsewhere.

Run the whole native set rather than a hand-picked subset - it is a few minutes, and there is no reliable way to guess which checks a change touches. Enumerate them from `nix flake show --json` and build each one. Editing anything under `modules/src/` is the case that punishes guessing: it breaks TWO committed fixtures plus the generated module, and each is a separate command (`python3 tests/test_agentbox.py --update`, `nix run .#update-golden`, `nix run .#assemble`). Discovering them one at a time costs a red CI round each; PR #472 spent two that way. Pass `--keep-going`, or `nix build` lists every not-yet-built check beside the one that really failed and those have no build log.
- `cfn-lint aws/template.yaml aws/lightsail-template.yaml` validates the CloudFormation templates.
- `python3 scripts/check_azure_template.py` validates the Azure deployment: that `azure/agent-box.json` still matches a fresh `bicep build` of `azure/agent-box.bicep` (the JSON is a build artifact and is what the README's Deploy button serves, so an edit that never reached it ships nothing), that the bootstrap the template carries renders with no placeholder left, stays ASCII and parses under `bash -n`, and that the web password never leaves the extension's `protectedSettings` for the API-readable `settings`. Pass `--no-build` to skip the recompile on a machine with no Bicep CLI. Recompile with `cd azure && az bicep build --file agent-box.bicep`.

Prefer targeted checks over `nix flake check`; the intentionally filesystem-free VM configuration makes the latter unsuitable. Live browser tests require `E2E_BASE_URL` and `E2E_PASSWORD`; run `playwright test -c tests/e2e` after provisioning the nixpkgs Playwright browsers described in the config.

## A systemd unit that changes its own definition must opt out of restart-on-activation

`systemd.services.agent-box-update`'s environment bakes in `CURRENT_REV`
(`cfg.selfUpdate.rev`), which the unit's own script advances before calling
`nixos-rebuild switch`. So on every non-noop run, this unit's definition
necessarily differs from the one `switch-to-configuration` is activating
FROM -- and by NixOS's default `restartIfChanged = true`, activation sends
the stop signal to the cgroup the still-running script sits in, right as the
new generation finishes coming up. The rebuild has already succeeded by
then; only the unit's own exit status is a casualty (`Main process exited,
code=killed, status=15/TERM`, `Failed with result 'signal'`) -- a box that
just updated cleanly reports "Update failed" on the settings page. Same
false-negative mechanism as the amazon-init reports in #186 ("the switch
masked the unit that was performing the switch"), here landing on this
unit's own reporting instead of a boot-time test assertion. Confirm a given
"failure" is this and not a real one by comparing
`nixos-rebuild list-generations` against the journal: if a new, current
generation landed at the exact failure timestamp, the update applied.

Fixed in #514 with `restartIfChanged = false; stopIfChanged = false;` on the
unit -- an in-flight run is left alone, and the next trigger picks up the new
unit definition from the activated system regardless, so there is nothing to
gain from restarting a run already in progress. Any other unit whose own
definition is a function of what its own script just wrote (a rev, a
generated pin, a computed hash) needs the same two lines, for the same
reason.

## Trying a terminal option without a rebuild

ttyd's client merges URL query parameters into its client options on every
connect — `parseOptsFromUrlQuery` feeds `applyPreferences`, whose default arm
assigns straight onto `terminal.options`. So any xterm.js option can be tried
on a running box before it is committed as a `-t key=value` flag on ttyd's
ExecStart:

    https://<box>/<user>/<session>/?macOptionClickForcesSelection=true

Use a per-session URL: the tabbed workspace at `/<user>/` embeds the terminal
in an iframe whose `src` carries no query. Server-side the extra parameter is
inert — under `--url-arg` ttyd consumes only `arg=` fragments, which is how
Caddy passes the session name — and the browser console logs
`[ttyd] option: <key>=<value>` when one lands. This is how the Mac
Option-drag selection flag (issue #327) was confirmed on a real Mac before it
shipped.

The whole client bundle is inlined in ttyd's `index.html`, and ttyd listens on
localhost without Caddy's basic auth in front of it, so
`curl -s http://127.0.0.1:7681/<user>/` gets you the exact xterm.js a box is
serving. Read that before trusting upstream docs about which version does
what; `systemctl cat agent-web-terminal-<user>` gives the port and the flags
actually in force.

## Vendoring a third-party asset

The settings and workspace pages load **no external asset** — no CDN, no font
host, no second request — and a deployed box fetches `modules/agent-box.nix` as
a single file (issue #51). So a dependency the page needs cannot be linked; it
has to live in the repo and be embedded like every other payload. That is why
`modules/src/vendor/` exists, and why it is deliberately small: today it holds
one file, idiomorph (0BSD, 3.3 KB gzipped), which the live swap in
`modules/src/settings.js` morphs with.

Before adding a second one, weigh what it actually costs here, which is more
than the download: the bytes are inlined into **every** page response, and the
settings page re-fetches itself on every session change, so a 50 KB library is
50 KB per poll on a box someone reads from a phone. That is the reasoning that
kept htmx out and let idiomorph in — the useful 3 KB of that ecosystem without
the 50 KB of it.

The rules, all enforced by `scripts/check_vendor.py`:

- **Pin it.** Every file under `modules/src/vendor/` needs an entry in
  `vendor.json` recording repo, tag, exact source URL, sha256 and license. A
  vendored file with no pin fails the check.
- **Never edit a vendored file in place.** It is byte-identical to the artifact
  at its `url`, so the recorded hash is *upstream's* and anyone can re-derive
  it. A local patch makes that unverifiable. Patch upstream, or keep the change
  in our own code next to the include.
- **Bump with the tool**, never by hand:
  `python3 scripts/check_vendor.py --update NAME`, then `nix run .#assemble`,
  `python3 tests/test-assemble-module.py` and `nix run .#update-golden`.
- **Mind the escaping.** A vendored asset is included into a Python
  triple-quoted host in `settings-daemon.py`, so `bin/assemble-module.py`
  escapes it for that dialect the way it already did for Nix. Minified
  JavaScript is exactly what needs it — `/[\s\S]/` in a non-raw host loses its
  backslashes — and `tests/test-assemble-module.py` round-trips the committed
  vendored files through it so a bump that introduces something unrepresentable
  fails there rather than on a box.
- **Give it its own `<script>` element.** Concatenating a minified bundle above
  our own IIFE is one missing semicolon away from ASI gluing them into a call.

## Coding Style & Naming Conventions

Follow existing formatting: two-space indentation for Nix and TypeScript, four spaces for Python, and trailing semicolons in TypeScript. Use kebab-case for Nix check names and filenames, descriptive camelCase for Nix locals, and `UPPER_SNAKE_CASE` for environment variables. Keep comments focused on security constraints or non-obvious deployment behavior. No repository-wide formatter is configured, so match adjacent code.

**Keep Markdown and Python source files ASCII.** Write `-` and not an em dash,
`...` and not an ellipsis, straight quotes and not curly ones, `->` and not an
arrow. Unicode punctuation is fine in HTML shown in the browser, but spell it
with ASCII HTML entities such as `&mdash;` and `&hellip;`, or with an ASCII
escape in a Python string whose output is HTML. Do not use an escape to put
Unicode into a shell wrapper, systemd unit, journal line, hook-session prompt
or terminal output. One of those consumers is already ASCII-checked outright
(`scripts/check_azure_template.py` refuses a non-ASCII bootstrap). A JSON
manifest is the worst case: `\u2014` is invisible in the source and arrives as
an em dash in every file rendered from it, so a byte scan of the manifest finds
nothing while the output is full of them.

New and changed lines only - existing prose is not being rewritten, so match
the file around you for everything else.

### Shell in the payloads

The scripts under `modules/src/` run on every deployed box, so a shell subtlety
there ships as a bug rather than failing in review. The one that has already
bitten: **tab is IFS whitespace**, so `IFS="$(printf '\t')" read -r a b c d`
folds a run of tabs into a single delimiter and silently drops an empty field -
`x<TAB>y<TAB><TAB>1` assigns `c=1` and leaves `d` empty. Only non-whitespace IFS
characters preserve empty fields. That made `session-cli.sh peers` read every
registered session as "not in the registry" on PR #522, and it is invisible in
review because the `jq` above it emits the right number of fields. Give every
`@tsv` column a non-empty sentinel in the jq itself, restore it after the
`read`, and verify the parse against a fixture before believing it.

### Icons in the web UI

Every icon on the settings and workspace pages is inline SVG from
[Octicons](https://primer.style/octicons/) (MIT), sized in px by CSS. Take
new ones from that set and inline them next to `ICON_LOCK` in
`modules/src/settings-daemon.py`: a `viewBox="0 0 16 16"` path with
`fill="currentColor"`, pasted into the page so the response stays
self-contained (the pages load no external asset, and Caddy serves no icon
font).

Never spell an icon as a text glyph. It reads as the cheaper option and is
not: a code point carrying `Emoji_Presentation` — `U+2699` GEAR is the one
that bit us (PR #448) — is drawn from the platform's COLOUR emoji font on iOS and
Android, so the settings gear arrived as a shaded 3D emoji beside a flat
monochrome (i), visibly larger and in the wrong palette. `font-size` does
not fix that; it only scales the emoji, and which glyph a font hands back
is not ours to choose. A code point the platform has no glyph for at all
(`U+24D8` CIRCLED LATIN SMALL LETTER I, on a plain Linux font set) renders
as tofu. Neither failure is visible in the desktop browser the page tends to
get written in — the desktop merely looks a little uneven.

## Testing Guidelines

Use `pkgs.testers.runNixOSTest` for service and VM behavior; name tests after the capability under test. Use Playwright `*.spec.ts` files only for behavior requiring a real browser or deployed instance. Add regression coverage with each behavioral fix. There is no numeric coverage threshold; CI expects every relevant named flake check to pass.

A VM test script has a hard ceiling of 128 KiB. nixpkgs passes it to the driver build in one environment variable and Linux caps a single environment string at `MAX_ARG_STRLEN`, so the test past that limit fails to build with `error: executing '.../bin/bash': Argument list too long` and no VM boots -- nothing in that message names the test script or its size. The `testscript-fits` check asserts the limit a page early so the failure is legible. When it fires, the answer is not shorter comments: move the assertions that do not need a VM into a native `runCommand` check (`webhook-spawn-claim` is one such move, out of `tests/webhook.nix`), or split the test the way `tests/sessions-common.nix` split the session tests.

Never end a test pipeline with `grep -q`. The driver runs each command under `set -euo pipefail`, and `grep -q` exits on the FIRST match — the producer upstream then gets EPIPE, and its non-zero status fails the whole assertion even though the pattern matched (a `must succeed` that reports exit 123 with `write error: Broken pipe` in the log). Write `… | grep PATTERN >/dev/null` instead, which drains the input, or capture to a file first and grep the file. `grep -q` is safe only with a file operand.


### Before you push a VM test

The interactive tests are x86-only, so on an aarch64 box every mistake below
costs a full CI round of about twelve minutes instead of a few seconds. Each
one here has already cost at least one.

- **Run the driver's own gates natively.** nixpkgs type-checks and lints
  `testScript` while BUILDING the driver (`ty check`, then
  `ruff check --select F` over the prepended stubs and the script), and both
  kill the test before any guest boots. Extract the script, stub the
  driver-supplied names as `Any`, and run those two from the repo's pinned
  nixpkgs; `nix-instantiate --parse` and
  `nix eval .#checks.x86_64-linux.<test>.drvPath` cover the Nix layer. Two
  gotchas nothing else catches: a bare `''` inside the `''...''` testScript -
  an empty shell argument, say - CLOSES the string, so write `""`; and any
  `${...}` you mean the GUEST's shell to expand is a Nix antiquotation first,
  so escape it as `''${...}`.
- **The backdoor shell is a TTY.** `test-instrumentation.nix` runs it with
  `exec < /dev/hvc0 > /dev/hvc0`, so `[ -t 0 ]` is TRUE inside every
  `machine.succeed(...)` - including through a `su -c` - and nobody will ever
  type anything. A script that waits for a keypress hangs until the job
  timeout, which GitHub reports as `cancelled` rather than as a test failure
  (that is one 25-minute run on PR #452). Gate interactivity on the DEVICE
  instead - `readlink /proc/self/fd/0` matching `/dev/pts/*`, which ttyd and a
  real ssh get and the console, a pipe and `/dev/null` do not - and bound the
  wait anyway. To give a test a real pty, wrap the command in
  `script -q -c '<cmd>' /dev/null` and pipe a newline in.
- **The backdoor also sources `/etc/profile`, and `su` without `-l` passes the
  caller's environment straight through.** So `environment.variables` DOES
  reach a `su -s /bin/sh <user> -c '...'` command, even though that shell is
  neither a login nor an interactive one and sources nothing itself. This is
  how `programs.tmux`'s `TMUX_TMPDIR` reached `agent-box-session` and pointed
  it at `/run/user/0` (#267, fixed in #270). Probing with
  `env -i /bin/sh -c 'echo $VAR'` answers a different question, because the
  value is INHERITED rather than sourced.
- **`sudo -u agent` is not the agent's namespace.** It runs in the test
  driver's root mount namespace, so it proves Unix permissions and never
  touches `agent-box-agent.service`'s `ProtectSystem=strict` remount: anything
  gated by `ReadWritePaths` passes in the test and fails EROFS on a real box.
  That is exactly how the `~/sites` self-serve vhosts shipped broken with
  `tests/self-serve-domain.nix` green throughout - `ReadWritePaths` listed
  `/home/agent`, and the kernel matches the RESOLVED path of a symlink out to
  `/var/lib/agent-box-sites/<user>`. Join the unit instead:
  `nsenter -t $(systemctl show -p MainPID --value agent-box-agent.service) -m -- runuser -u agent -- <cmd>`.
- **`tests/memory-protection.nix` is the only test that leaves `web.enable` at
  its default (off)**, despite a name that suggests it only covers zram and
  earlyoom. It is therefore the sole guard against a broken DEFAULT
  deployment: referencing a path from the `web.enable`-gated block in the
  ungated per-user unit makes systemd fail namespace setup with 226/NAMESPACE
  on a web-less box, so the unit never starts and there are no sessions at all
  (shipped on master once, caught only as a 900s `wait_for_unit` timeout, and
  fixed in #198). When you touch the agent unit's sandbox paths, evaluate BOTH
  configurations before pushing - `t.nodes.<node>` reaches inside a
  `runNixOSTest` check, so any unit property can be read natively without
  booting anything.
- **Page assertions match prose AND attribute adjacency.** `tests/webhook.nix`,
  `tests/connect.nix`, `tests/settings-page.nix` and `tests/sessions-web.nix`
  all assert user-visible copy, so any PR that rewrites settings-page wording
  breaks them - and `sessions-web` matches whole runs of tab markup as one
  string, so inserting an attribute into `<a class="tab">` between `data-tab`
  and `title` reddens four assertions at once, on pages unrelated to the
  change (PR #522). Prefer a home that does not split an asserted run over
  loosening the assertions. The tests abort at the FIRST failing assert, so
  each fix reveals only the next one: #506's copy pass took #509, then #515,
  and would have taken a fourth, with master red the whole time. Sweep all
  four files after a copy change rather than pushing one-line fixes, render
  the function directly, and check every affected assertion against the real
  markup first. Treat the sweep as triage, not a gate - a test may assert the
  ESCAPED rendering (`Install &amp; sign in`) where the source holds
  `Install & sign in`, the daemon splits some strings across source lines,
  and a negative assert tests STATE rather than the presence of a string.

### Get the feedback loop off CI

- **Reproduce the `writePython3` gate locally.** `pkgs.writers.writePython3`
  and `writePython3Bin` run `py_compile` PLUS flake8 at build time, and
  `flakeIgnore` REPLACES pycodestyle's defaults - so W503 and W504, normally
  off, become errors the moment you pass a list. Compose the program exactly
  the way the module does (the seam matters: an off-by-one newline gives a
  bogus E303 that is not in the real build), run flake8 over it with the same
  `--max-line-length` and ignore list, then confirm against
  `nix build .#golden-snapshot`, which builds every generated payload.
  Splicing a shared `.py` into a host `.py` also re-binds its imports: expect
  F811 in the host's `flakeIgnore`, and E402 in any program whose own imports
  follow the spliced library.
- **`git add` a new file BEFORE any `nix build` or `nix eval`.** Nix copies
  only TRACKED files out of a dirty flake, so an untracked `modules/src/...`
  fails as "missing include target" or "path does not exist" and reads like a
  bug in your own code.
- **Never write `#!/usr/bin/env` in a script a test generates.** The Nix build
  sandbox has no `/usr/bin`, so the exec fails there while passing natively,
  and it surfaces as whatever the code under test says when its subprocess
  returns non-zero - "could not resolve profile", for the profile panel -
  which reads like a bug in the code rather than in the harness. Fill the
  shebang from `sys.executable`, and run a new check in the sandbox before
  believing a native `python3 tests/foo.py` pass.
- **Run the settings daemon on your own box before pushing a change to it or
  to its test.** Expand its `@@include:` markers into one file (the module
  PREPENDS `lib/envstore.py`, so concatenate the two first), start it on a
  PRIVATE tmux socket with `AGENT_BOX_SETTINGS_ENV_FILE` and
  `AGENT_BOX_SESSIONS_FILE` pointed at COPIES, point `AGENT_BOX_CONNECT_BINS`
  at stubs kept byte-equivalent to the ones in `tests/*.nix`, and then walk
  the VM test's subtests IN ORDER, asserting the state after each. Three CI
  rounds on PR #317 went to things a twenty-second local replay finds. Start
  it under `env -u GH_TOKEN -u GITHUB_TOKEN`: the sign-in probe inherits
  `os.environ`, so your own token makes a card read "signed in" where the VM's
  would not.
- **To prove a click handler works, drive a real headless chromium over CDP**
  rather than curling HTML. Two settings are the difference between a passing
  clipboard test and a silent failure that looks like a broken handler:
  `Runtime.evaluate` needs `userGesture: true`, or `clipboard.writeText`
  rejects `NotAllowedError` for want of transient activation, and
  `Browser.grantPermissions` with `clipboardReadWrite` is what lets you read
  back what the button copied (PR #421). For a SCREENSHOT, never point
  chromium at the live page or the rig: the page opens an SSE stream that
  never ends, so headless chromium never fires load and hangs past any
  timeout. Curl the page to a file and shoot `file://`, where the inline CSS
  and JS still render and only the SSE fails fast.
- **Never read a status through a pipe.** `cmd 2>&1 | tail -N; echo $?`
  reports `tail`'s status, which is almost always 0 - and nix prints
  `building '...drv'` lines whether or not the build succeeded, with the
  failure text above the tail window. That is how PR #488 was reported as "all
  24 aarch64 checks green" twice while `backend-parity` was red, and CI had to
  correct it. Any time a run becomes a claim to somebody else, capture the
  status without a pipe first, then look at the output.

## Filing Issues, and when to skip straight to the PR

When you hit something wrong - a bug, a design gap, a stale doc, a flaky check, surprising behavior you had to work around - do not let it die in a session transcript: the next agent starts with none of your context. But an issue is not automatically the right container for it. This is a repo we control, so **when you know the fix and can push it, open the PR instead**; an issue that already carries the diagnosis and the patch is churn, costing a read for every future triager and giving nothing the PR does not carry. Name the symptom in the PR body, so the work is still findable by what went wrong.

Size decides which one you write. File an issue when the work is genuinely undecided (two or more designs with real trade-offs), when the fix belongs to a repo or a person we do not control, or when the change is too large to do now and the context would otherwise be lost. A mechanism ask upstream stays an issue, because that is a real decision for somebody else. Do either one mid-task on something unrelated, do it even when you already worked around the problem, and do not ask permission for either.

Either way it belongs in the repo that owns the fix, not the one you happen to be sitting in (`defangdevs/local-channels` for local-webhook behavior, `defangdevs/agent-box` for the module and the box), and cross-link when a symptom and its fix live in different repos. Write down what you observed, the smallest reproduction you have, and where you think the fix belongs; if you considered several approaches, list them with a recommendation rather than leaving the next reader to re-derive them. Search the open issues first — add to an existing one instead of opening a near-duplicate. When a PR only papers over the underlying problem, say so in the PR and link the issue.

## Commit & Pull Request Guidelines

History uses concise imperative subjects and scoped Conventional Commit forms such as `feat(web): ...`, `fix(sessions): ...`, and `docs(agents-md): ...`. Reference issues when applicable. Pull requests should explain motivation, summarize user-visible and security effects, list exact checks run, and link the issue. Include screenshots for changes to the workspace or settings UI, and call out AWS cost, IAM, networking, or migration impacts.

Attach those screenshots with `agent-box-upload FILE --repo defangdevs/agent-box`, which prints the markdown to paste — the same store the browser's paste uses, so no binary lands in the diff. The URL it prints answers 404 until a comment references it and then goes live seconds after you post, so a link that looks dead before you submit is not evidence of a failed upload. Do **not** add another `assets/*` branch: PRs #210, #228 and #243 each parked PNGs on one to keep the diff clean, each said "delete after merge", and all four branches (`assets/175-new-session-ui`, `assets/227-webhook-subscriptions-panel`, `assets/236-long-session-names`, `assets/241-stopped-session-ui`) are still here. Leave them, and leave the `defangdevs/assets` repo — closed PRs #133 and #209 embed images from them, and deleting either breaks that history — but do not add to either.

### Landing a PR

**The branch ruleset requires no status check.** It gates on resolved
conversations and zero approvals, and nothing else - so
`gh pr merge NNN --squash --auto` is not a promise to wait for green here. It
is an immediate merge with extra steps: PR #456 merged on the spot with its CI
run still `IN_PROGRESS`. To land on green, poll the run and merge once it
passes, or add the check to the ruleset first. Read `rules/branches` before
assuming any gate exists; the classic branch-protection API does not describe
this repo. This is also how stale page-copy assertions reach master at all.

**A CONFLICTING PR reports "no checks reported", not a failure.** The workflows
trigger on `pull_request`, which builds the MERGE commit, and a conflicted PR
has none - so GitHub queues nothing and reports absence rather than red. Check
`gh pr view NNN --json mergeable,mergeStateStatus` before waiting on CI; PR #478
looked green-by-absence for twenty minutes while master moved five commits
underneath it. The same mechanism cuts the other way when you are hunting a
regression: a PR whose branch predates the suspect commit still TESTS with it,
so a failure there is never evidence that the suspect is innocent. Compare
`gh api repos/O/R/pulls/N --jq .base.sha`.

**Give a long job a STEP-level `timeout-minutes` under the job's.** A job that
exceeds its own timeout is reported `cancelled` on `workflow_run`,
`workflow_job` and `check_run` alike - byte-identical to what a
`cancel-in-progress` supersede produces, with no payload field separating the
two, so the standing watch cannot match it and a hung run reaches nobody. A
STEP that runs out of time fails instead, which the existing rules do match.
Size the step budget against the PRE-step time rather than the job timeout:
setup before `Run VM tests` takes up to 219s, so a 25-minute job leaves about
21 minutes of step budget (PR #456, at 18).

### Finishing a CodeRabbit review

CodeRabbit reviews every PR in this repo, and pushing the fix does not clear the review it left. Two things stay behind, and each one keeps the PR red on its own - `gh pr merge --auto` then waits forever, because the branch ruleset gates on resolved conversations. Close both by hand, in this order, as the last step of addressing a review:

1. **Resolve every thread you addressed.** Re-read the thread first: a finding can be argued down and withdrawn between the moment you read it and the moment you push, and resolving is the wrong answer to a comment you decided not to act on - reply there instead. Collect the ids and resolve them, paginating past the first 100 threads:

       gh api graphql --paginate --slurp -f query='
         query($endCursor: String) {
           repository(owner:"defangdevs", name:"agent-box") {
             pullRequest(number: NNN) {
               reviewThreads(first: 100, after: $endCursor) {
                 nodes { id isResolved path line }
                 pageInfo { hasNextPage endCursor }
               }
             }
           }
         }' | jq -r '.[].data.repository.pullRequest.reviewThreads.nodes[].id'
       gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"THREAD_ID"}){thread{isResolved}}}'

2. **Dismiss the review itself.** A `CHANGES_REQUESTED` verdict outlives both the resolved threads and the new commits, so the PR keeps showing changes requested until the review is dismissed:

       gh api -X PUT repos/defangdevs/agent-box/pulls/NNN/reviews/REVIEW_ID/dismissals \
         -f event=DISMISS -f message='addressed in <sha>'

   `gh pr view NNN --json reviews` has the review ids and their states. A plain `COMMENTED` review carries no merge-blocking verdict, so there is nothing here to clear - asking for a fresh pass with an `@coderabbitai review` comment gets the same result the slow way.
