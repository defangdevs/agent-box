# Repository Guidelines

## Project Structure & Module Organization

`modules/agent-box.nix` is the portable NixOS module and the repository's main implementation. It is **generated** — do not edit it by hand. The sources are `modules/agent-box.nix.in` (the Nix template) plus the assets under `modules/src/` (e.g. the settings daemon), stitched together by `bin/assemble-module.py` via `@@include:...@@` markers. `flake.nix` exposes the module, VM image, and CI checks. Host examples live in `hosts/`; AWS deployment configuration and operational notes are in `aws/`. Put NixOS integration tests in `tests/*.nix`, live browser tests in `tests/e2e/*.spec.ts`, maintenance utilities in `scripts/`, and website images or static content in `docs/`.

Keep the module self-contained: deployed boxes fetch `modules/agent-box.nix` as a single file, so it must not import sibling files. This is why the sources are re-embedded at build time rather than loaded with `readFile` — after editing the template or `modules/src/`, run `nix run .#assemble` and commit the regenerated `modules/agent-box.nix` (CI's `module-generated-up-to-date` check fails on drift).

## Build, Test, and Development Commands

- `nix run .#assemble` regenerates `modules/agent-box.nix` from `modules/agent-box.nix.in` + `modules/src/` (run from the repo root after editing either).
- `nix build -L .#checks.x86_64-linux.module-generated-up-to-date` verifies the committed module matches its sources.
- `nix flake metadata` validates flake inputs and basic evaluation.
- `nix build .#vm` builds the bootable qcow2 image under `result/`.
- `nix build -L .#checks.x86_64-linux.multi-user` runs the quick module/configuration assertion.
- `nix build -L .#checks.x86_64-linux.module-single-file` verifies standalone module evaluation.
- `nix build -L .#checks.x86_64-linux.<name>` runs an individual VM test such as `sessions` or `settings-page`.
- `cfn-lint aws/template.yaml` validates the CloudFormation template.

Prefer targeted checks over `nix flake check`; the intentionally filesystem-free VM configuration makes the latter unsuitable. Live browser tests require `E2E_BASE_URL` and `E2E_PASSWORD`; run `playwright test -c tests/e2e` after provisioning the nixpkgs Playwright browsers described in the config.

## Coding Style & Naming Conventions

Follow existing formatting: two-space indentation for Nix and TypeScript, four spaces for Python, and trailing semicolons in TypeScript. Use kebab-case for Nix check names and filenames, descriptive camelCase for Nix locals, and `UPPER_SNAKE_CASE` for environment variables. Keep comments focused on security constraints or non-obvious deployment behavior. No repository-wide formatter is configured, so match adjacent code.

## Testing Guidelines

Use `pkgs.testers.runNixOSTest` for service and VM behavior; name tests after the capability under test. Use Playwright `*.spec.ts` files only for behavior requiring a real browser or deployed instance. Add regression coverage with each behavioral fix. There is no numeric coverage threshold; CI expects every relevant named flake check to pass.

## Commit & Pull Request Guidelines

History uses concise imperative subjects and scoped Conventional Commit forms such as `feat(web): ...`, `fix(sessions): ...`, and `docs(agents-md): ...`. Reference issues when applicable. Pull requests should explain motivation, summarize user-visible and security effects, list exact checks run, and link the issue. Include screenshots for changes to the workspace or settings UI, and call out AWS cost, IAM, networking, or migration impacts.
