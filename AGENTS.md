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
sits in. Editing the shipped guide is a module change like any other — run
`nix run .#assemble`, and the golden fixture moves with it.

## Project Structure & Module Organization

`modules/agent-box.nix` is the portable NixOS module and the repository's main implementation. It is **generated** — do not edit it by hand. The sources are `modules/agent-box.nix.in` (the Nix template) plus the assets under `modules/src/` (e.g. the settings daemon), stitched together by `bin/assemble-module.py` via `@@include:...@@` markers. `flake.nix` exposes the module, VM image, and CI checks. Host examples live in `hosts/`; AWS deployment configuration and operational notes are in `aws/`. Put NixOS integration tests in `tests/*.nix`, live browser tests in `tests/e2e/*.spec.ts`, maintenance utilities in `scripts/`, and website images or static content in `docs/`.

Keep the module self-contained: deployed boxes fetch `modules/agent-box.nix` as a single file, so it must not import sibling files. This is why the sources are re-embedded at build time rather than loaded with `readFile` — after editing the template or `modules/src/`, run `nix run .#assemble` and commit the regenerated `modules/agent-box.nix` (CI's `module-generated-up-to-date` check fails on drift).

## Build, Test, and Development Commands

- `nix run .#assemble` regenerates `modules/agent-box.nix` from `modules/agent-box.nix.in` + `modules/src/` (run from the repo root after editing either).
- `nix build -L .#checks.<system>.module-generated-up-to-date` verifies the committed module matches its sources (`<system>` is your host's, e.g. `aarch64-linux`).
- `nix build -L .#checks.<system>.assemble-module-escaping` runs `tests/test-assemble-module.py`, the unit tests for the assembler's Nix escaping (issue #244). The up-to-date check above cannot catch an escaping bug — it regenerates the file with the same assembler, so the check and the bug agree on the wrong bytes. Runnable without Nix too: `python3 tests/test-assemble-module.py`, or `--nix` to round-trip the corpus through `nix-instantiate` instead of the lexer model.
- `nix flake metadata` validates flake inputs and basic evaluation.
- `nix build .#packages.x86_64-linux.vm` builds the bootable qcow2 image under `result/`.
- `nix build -L .#checks.<system>.multi-user` runs the quick module/configuration assertion.
- `nix build -L .#checks.<system>.golden-snapshot` verifies the rendered configuration (every generated unit, /etc file, tmpfiles rule and script payload, store hashes normalized) still byte-matches the committed `tests/golden/` fixture — the behavior lock for the issue #154 portability refactor. After an INTENTIONAL behavior change (or a flake.lock bump, which changes store path names), run `nix run .#update-golden` and commit the reviewed `tests/golden/` diff together with the change.
- `nix build -L .#checks.<system>.module-single-file` verifies standalone module evaluation.
- `nix build -L .#checks.x86_64-linux.<name>` runs an individual VM test such as `sessions` or `settings-page`.

The eval-level checks (`multi-user`, `module-single-file`, `download-route`, `webhook-route`, `module-generated-up-to-date`, `assemble-module-escaping`) and `nix run .#assemble` are exposed for both `x86_64-linux` and `aarch64-linux`, so they run natively on a Graviton box like the deployed fleet. The qcow2 image and the interactive `runNixOSTest` checks are `x86_64-linux`-only: a NixOS test needs a same-arch KVM guest, so building them from another arch would fall back to unusably slow TCG. Widen `vmSystems` in `flake.nix` to offer them elsewhere.
- `cfn-lint aws/template.yaml aws/lightsail-template.yaml` validates the CloudFormation templates.

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

## Coding Style & Naming Conventions

Follow existing formatting: two-space indentation for Nix and TypeScript, four spaces for Python, and trailing semicolons in TypeScript. Use kebab-case for Nix check names and filenames, descriptive camelCase for Nix locals, and `UPPER_SNAKE_CASE` for environment variables. Keep comments focused on security constraints or non-obvious deployment behavior. No repository-wide formatter is configured, so match adjacent code.

## Testing Guidelines

Use `pkgs.testers.runNixOSTest` for service and VM behavior; name tests after the capability under test. Use Playwright `*.spec.ts` files only for behavior requiring a real browser or deployed instance. Add regression coverage with each behavioral fix. There is no numeric coverage threshold; CI expects every relevant named flake check to pass.

Never end a test pipeline with `grep -q`. The driver runs each command under `set -euo pipefail`, and `grep -q` exits on the FIRST match — the producer upstream then gets EPIPE, and its non-zero status fails the whole assertion even though the pattern matched (a `must succeed` that reports exit 123 with `write error: Broken pipe` in the log). Write `… | grep PATTERN >/dev/null` instead, which drains the input, or capture to a file first and grep the file. `grep -q` is safe only with a file operand.

## Filing Issues

When you hit something wrong — a bug, a design gap, a stale doc, a flaky check, surprising behavior you had to work around — **file an issue**, then carry on with what you were doing. Do it even when you are mid-task on something unrelated, even when you already worked around it, and even when it is small: a session transcript is not a bug tracker, and the next agent starts with none of your context.

File it in the repo that owns the fix, not the one you happen to be sitting in (`defangdevs/local-channels` for local-webhook behavior, `defangdevs/agent-box` for the module and the box), and cross-link when a symptom and its fix live in different repos. Write down what you observed, the smallest reproduction you have, and where you think the fix belongs; if you considered several approaches, list them with a recommendation rather than leaving the next reader to re-derive them. Search the open issues first — add to an existing one instead of opening a near-duplicate. When a PR only papers over the underlying problem, say so in the PR and link the issue.

## Commit & Pull Request Guidelines

History uses concise imperative subjects and scoped Conventional Commit forms such as `feat(web): ...`, `fix(sessions): ...`, and `docs(agents-md): ...`. Reference issues when applicable. Pull requests should explain motivation, summarize user-visible and security effects, list exact checks run, and link the issue. Include screenshots for changes to the workspace or settings UI, and call out AWS cost, IAM, networking, or migration impacts.
