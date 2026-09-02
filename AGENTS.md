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
the user to download?" belongs in the shipped guide. A box that also has a
clone of this repo ends up with both, discovered at different scopes: the
shipped guide through the pointer seeded in `$HOME` (and, for claude,
`~/.claude/CLAUDE.md` -> the `/etc` guide), this file through the checkout it
sits in — which, since #242, every NixOS box has by default
(`selfUpdate.checkout`), so "both, at different scopes" is now the
ordinary case rather than the one our own box happens to be in. Editing the shipped guide is a module change like any other — run
`nix run .#assemble`, and the golden fixture moves with it.

## Project Structure & Module Organization

`modules/agent-box.nix` is the portable NixOS module and the repository's main implementation. It is **generated** — do not edit it by hand. The sources are `modules/agent-box.nix.in` (the Nix template) plus the assets under `modules/src/` (e.g. the settings daemon), stitched together by `bin/assemble-module.py` via `@@include:...@@` markers. `flake.nix` exposes the module, VM image, and CI checks. Host examples live in `hosts/`; AWS deployment configuration and operational notes are in `aws/`, and the Azure equivalent in `azure/` (Bicep source plus its compiled ARM JSON, which is a build artifact - see `azure/README.md`). Put NixOS integration tests in `tests/*.nix`, live browser tests in `tests/e2e/*.spec.ts`, maintenance utilities in `scripts/`, and website images or static content in `docs/`.

Keep the module self-contained: deployed boxes fetch `modules/agent-box.nix` as a single file, so it must not import sibling files. This is why the sources are re-embedded at build time rather than loaded with `readFile` — after editing the template or `modules/src/`, run `nix run .#assemble` and commit the regenerated `modules/agent-box.nix` (CI's `module-generated-up-to-date` check fails on drift).

## Build, Test, and Development Commands

- `nix run .#assemble` regenerates `modules/agent-box.nix` from `modules/agent-box.nix.in` + `modules/src/` (run from the repo root after editing either).
- `nix build -L .#checks.<system>.module-generated-up-to-date` verifies the committed module matches its sources (`<system>` is your host's, e.g. `aarch64-linux`).
- `nix build -L .#checks.<system>.assemble-module-escaping` runs `tests/test-assemble-module.py`, the unit tests for the assembler's Nix escaping (issue #244). The up-to-date check above cannot catch an escaping bug — it regenerates the file with the same assembler, so the check and the bug agree on the wrong bytes. Runnable without Nix too: `python3 tests/test-assemble-module.py`, or `--nix` to round-trip the corpus through `nix-instantiate` instead of the lexer model.
- `nix build -L .#checks.<system>.backend-parity` compares what the NixOS module and the native renderer supply to the SAME shared payloads under `modules/src/` — every `AGENT_BOX_*` variable and every rendered service — and fails on a difference not declared in `scripts/check_backend_parity.py`. Runnable without Nix too: `python3 scripts/check_backend_parity.py`. Its two tables are the point: `BY_DESIGN` for a difference that is correct (with the reason), `KNOWN_GAPS` for one that is a bug (with its issue). Both are staleness-checked, so a gap you fix must be deleted from the table in the same change. Issue #392 — the settings page's Connections section, missing from every native box because only the module set `AGENT_BOX_CONNECT_BINS` — is the failure this exists to catch; neither backend's fixture could see it, because each ratifies whatever its own renderer emits.
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
- `cfn-lint aws/template.yaml aws/lightsail-template.yaml` validates the CloudFormation templates.
- `python3 scripts/check_azure_template.py` validates the Azure deployment: that `azure/agent-box.json` still matches a fresh `bicep build` of `azure/agent-box.bicep` (the JSON is a build artifact and is what the README's Deploy button serves, so an edit that never reached it ships nothing), that the bootstrap the template carries renders with no placeholder left, stays ASCII and parses under `bash -n`, and that the web password never leaves the extension's `protectedSettings` for the API-readable `settings`. Pass `--no-build` to skip the recompile on a machine with no Bicep CLI. Recompile with `cd azure && az bicep build --file agent-box.bicep`.

Prefer targeted checks over `nix flake check`; the intentionally filesystem-free VM configuration makes the latter unsuitable. Live browser tests require `E2E_BASE_URL` and `E2E_PASSWORD`; run `playwright test -c tests/e2e` after provisioning the nixpkgs Playwright browsers described in the config.

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

**Keep it ASCII.** Write `-` and not an em dash, `...` and not an ellipsis,
straight quotes and not curly ones, `->` and not an arrow. This is about where
these strings END UP, not about the editor they are written in: comments and
prose in `modules/src/` are embedded into generated shell wrappers, systemd
units, journal lines, hook-session prompts and the terminal a user reads over
ttyd, and one of those consumers is already ASCII-checked outright
(`scripts/check_azure_template.py` refuses a non-ASCII bootstrap). A JSON
manifest is the worst case: `\u2014` is invisible in the source and arrives as
an em dash in every file rendered from it, so a byte scan of the manifest finds
nothing while the output is full of them.

New and changed lines only - existing prose is not being rewritten, so match
the file around you for everything else.

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

Never end a test pipeline with `grep -q`. The driver runs each command under `set -euo pipefail`, and `grep -q` exits on the FIRST match — the producer upstream then gets EPIPE, and its non-zero status fails the whole assertion even though the pattern matched (a `must succeed` that reports exit 123 with `write error: Broken pipe` in the log). Write `… | grep PATTERN >/dev/null` instead, which drains the input, or capture to a file first and grep the file. `grep -q` is safe only with a file operand.

## Filing Issues

When you hit something wrong — a bug, a design gap, a stale doc, a flaky check, surprising behavior you had to work around — **file an issue**, then carry on with what you were doing. Do it even when you are mid-task on something unrelated, even when you already worked around it, and even when it is small: a session transcript is not a bug tracker, and the next agent starts with none of your context.

File it in the repo that owns the fix, not the one you happen to be sitting in (`defangdevs/local-channels` for local-webhook behavior, `defangdevs/agent-box` for the module and the box), and cross-link when a symptom and its fix live in different repos. Write down what you observed, the smallest reproduction you have, and where you think the fix belongs; if you considered several approaches, list them with a recommendation rather than leaving the next reader to re-derive them. Search the open issues first — add to an existing one instead of opening a near-duplicate. When a PR only papers over the underlying problem, say so in the PR and link the issue.

## Commit & Pull Request Guidelines

History uses concise imperative subjects and scoped Conventional Commit forms such as `feat(web): ...`, `fix(sessions): ...`, and `docs(agents-md): ...`. Reference issues when applicable. Pull requests should explain motivation, summarize user-visible and security effects, list exact checks run, and link the issue. Include screenshots for changes to the workspace or settings UI, and call out AWS cost, IAM, networking, or migration impacts.

Attach those screenshots with `agent-box-upload FILE --repo defangdevs/agent-box`, which prints the markdown to paste — the same store the browser's paste uses, so no binary lands in the diff. Do **not** add another `assets/*` branch: PRs #210, #228 and #243 each parked PNGs on one to keep the diff clean, each said "delete after merge", and all four branches (`assets/175-new-session-ui`, `assets/227-webhook-subscriptions-panel`, `assets/236-long-session-names`, `assets/241-stopped-session-ui`) are still here. Leave them, and leave the `defangdevs/assets` repo — closed PRs #133 and #209 embed images from them, and deleting either breaks that history — but do not add to either.
