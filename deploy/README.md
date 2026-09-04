# Deployment templates

One directory per cloud provider. Each holds that provider's 1-click
template, its build artifacts, and a README with the design notes,
the cost breakdown and what to do when a deployment fails.

| Directory | Provider | Template source | Serves the launch button from |
|---|---|---|---|
| [`aws/`](./aws/README.md) | AWS | `template.yaml` (EC2), `lightsail-template.yaml` (Lightsail) | S3, pinned to the publishing commit by `publish-template.yml` |
| [`azure/`](./azure/README.md) | Azure | `agent-box.bicep` → `agent-box.json` | `raw.githubusercontent.com` at `master`, unpinned |

The templates are the *deployment* layer only. What they install is the
same everywhere: `modules/agent-box.nix` on a NixOS box, or `agentbox
apply` against `/etc/agent-box/config.yaml` on a native one. A behavior
change belongs in `modules/`, not here — a template that grows its own
policy is a box that behaves differently depending on which cloud it
was launched in.

A new provider gets a sibling directory here, not a new top-level one.
Give it a README with the same sections as the two above, and add its
CI workflow to `.github/workflows/` keyed on `deploy/<provider>/**`.
