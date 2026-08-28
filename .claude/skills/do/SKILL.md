---
description: End-to-end task execution for agent-box — research, plan, implement, review, and merge via PR. Tracked entirely in the GitHub issue/PR; no local state file.
argument-hint: <task description, or an existing issue number>
---

# /do

## Input

```text
$ARGUMENTS
```

You are an autonomous task executor for `defangdevs/agent-box`. Take the task above from idea to merged PR with minimal hand-holding, following every phase below in order.

**Why there is no local state file:** `$HOME` on an agent-box deployment is shared by every concurrent session and survives reboots — it is not the disposable, single-task filesystem a hidden checklist file assumes. A stray state file can go stale, get picked up by the wrong session, or outlive a crash with nothing to clean it up. GitHub — the issue, then the PR — is the only place state can live without those risks. A `TodoWrite` list is fine as an in-session view of the phases below, but it is never the record of truth and does not need to survive a session restart.

## Phase 0 — Find or create the tracking issue

1. If the user gave an issue number, use it (`gh issue view <n> --repo defangdevs/agent-box`).
2. Otherwise search open issues for a match (`gh issue list --repo defangdevs/agent-box --search "..."`). Only open a new one if nothing fits.
3. Assign yourself: `gh issue edit <n> --repo defangdevs/agent-box --add-assignee @me`.
4. Build a `TodoWrite` list mirroring Phases 1–7 — your in-session view only.
5. Post the plan as an issue comment before writing any code. After this, phase-boundary updates go into the issue body (or the PR body once one exists) as edited checklist items — edits don't notify watchers the way new comments do. Reserve new comments for real milestones: plan posted, PR opened, PR merged.

## Phase 1 — Research

1. Read `AGENTS.md` and the [wiki](https://github.com/defangdevs/agent-box/wiki) — required before designing anything here, per `AGENTS.md`'s own Documentation Map. Check **Users-vs-Sessions** first if the task implies per-session isolation.
2. Search issues and merged PRs for prior art — this repo's issue numbers carry the "why" for a lot of non-obvious behavior (`AGENTS.md` itself cites #140, #154, #244, #392 inline).
3. Work out what the task touches:
   - `modules/agent-box.nix.in` / `modules/src/**` → the generated module; needs `nix run .#assemble` after editing.
   - `aws/template.yaml` / `aws/lightsail-template.yaml` → needs `cfn-lint` and `scripts/check_lightsail_userdata.py`.
   - Auth, secrets, webhook routing, sudo rules, Caddy basic-auth → flag for the security-review pass in Phase 5.
4. Update the issue body with what you found and a concrete implementation checklist.

## Phase 2 — Worktree

```bash
git worktree add ../agent-box-<slug> -b <type>/<issue>-<slug> origin/master
```

Use a Conventional Commit type prefix (`feat/`, `fix/`, `docs/`) matching what you'll ship. Work only in the worktree; never commit directly to `master`.

## Phase 3 — Implementation

- Follow `AGENTS.md` conventions: two-space indent (Nix/TypeScript), four-space (Python), kebab-case Nix check names, `UPPER_SNAKE_CASE` env vars.
- Touched `modules/agent-box.nix.in` or `modules/src/**`? Run `nix run .#assemble` and commit the regenerated `modules/agent-box.nix` in the same change — CI's `module-generated-up-to-date` check fails on drift.
- Intentional behavior change, or `flake.lock` moved? Run `nix run .#update-golden` and commit the reviewed `tests/golden/` diff alongside it.
- Push after every meaningful unit of work; update the issue/PR checklist after each push.
- Hit something wrong but unrelated (a bug, a stale doc, a flaky check)? File it as its own issue immediately per `AGENTS.md`'s Filing Issues section, cross-link it, and keep going — don't fold an unrelated fix into this PR.

## Phase 4 — Pre-PR validation

Run the targeted checks that match what you touched — **not** `nix flake check`, which `AGENTS.md` calls out as unsuitable here:

- `nix build -L .#checks.<system>.module-generated-up-to-date` and `...assemble-module-escaping` for module changes.
- `nix build -L .#checks.<system>.backend-parity` when both the NixOS module and the native renderer supply the same payload.
- `nix build -L .#checks.<system>.golden-snapshot` for any rendered-config change.
- `nix build -L .#checks.x86_64-linux.<name>` for the specific VM test(s) covering your change (e.g. `sessions`, `settings-page`).
- `cfn-lint aws/template.yaml aws/lightsail-template.yaml` plus `python3 tests/test_agentbox.py` for AWS/native-renderer changes.
- `playwright test -c tests/e2e` (needs `E2E_BASE_URL`/`E2E_PASSWORD`) only for behavior that needs a real browser.

Fix failures before proceeding, then check off validation in the issue/PR body.

## Phase 5 — Review

| Touches | Reviewer | Checks |
|---|---|---|
| Always | `/code-review` | correctness, reuse/simplification, efficiency |
| `**/auth*`, `**/secret*`, `**/*token*`, webhook routing, sudo rules, Caddy auth config | `/security-review` | credential handling, auth bypass, OWASP |
| `aws/**`, anything with IAM/networking impact | manual self-check | cost, IAM, networking, migration impact — call these out explicitly in the PR body per `AGENTS.md` |

**Hard stop:** don't open the PR until every triggered reviewer is addressed. Record PASS/ADDRESSED against each in the issue/PR body.

## Phase 6 — Verify

agent-box has no separate staging app — verify against a real box:

- For a terminal/session/webhook-facing change, try it live first where the box supports that (e.g. the query-parameter trick in `AGENTS.md`'s "Trying a terminal option without a rebuild"), or build the VM image (`nix build .#packages.x86_64-linux.vm`) and boot it.
- For workspace/settings UI changes, take screenshots and attach them with `agent-box-upload FILE --repo defangdevs/agent-box` — never a throwaway `assets/*` branch (`AGENTS.md` lists four still-open ones left over from exactly that shortcut).

## Phase 7 — PR & merge

1. Open the PR referencing the issue (`Fixes #<n>` or `Ref #<n>`). Body: motivation, user-visible and security effects, exact checks run, screenshots if UI, AWS cost/IAM/networking/migration callouts if relevant.
2. From here the PR body is the checklist of record — carry over anything still open from the issue body and stop editing the issue directly.
3. Push, wait for CI, fix any red checks.
4. Once green and every Phase 5 reviewer is addressed: self-merging is established practice in this repo. Match the existing merge method in recent history.
5. Clean up: `git worktree remove <path>`, confirm the tracking issue auto-closed from `Fixes #n` (close it manually if it didn't), `git pull origin master` in the main checkout.

## Guiding principles

- State lives in GitHub, never in a local file — this box's `$HOME` is shared and persistent, not disposable.
- Autonomy: complete the flow without asking unless genuinely blocked.
- Everything unrelated you hit gets its own issue immediately — not a mental note for later.
- A shortcut now is a bug someone else inherits later.
