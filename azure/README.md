# Azure deployment (`azure/`)

A Bicep template that provisions a single-agent agent-box host with a browser
terminal (Caddy -> ttyd) on one Azure Linux VM. The deployment form lets the
user choose Claude Code or Codex.

| File | Role |
| --- | --- |
| `agent-box.bicep` | **Canonical source.** Edit this. |
| `agent-box.json` | Build artifact of `az bicep build`. Do not edit by hand — it is what the Deploy button serves. |
| `.bicep-version` | Bicep CLI version CI compiles with. |

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fdefangdevs%2Fagent-box%2Fmaster%2Fazure%2Fagent-box.json)

## What the template does

- Provisions a vnet (`10.42.0.0/16`, one `/24` subnet — same CIDR as the AWS
  template, so the two are diffable side by side), an NSG, and a **Standard
  SKU static public IPv4**. Static because the box's hostname is derived from
  the address: a changed IP is a changed URL and a certificate that no longer
  matches.
- Launches one VM from the current Canonical Ubuntu 24.04 image
  (`server-arm64`, or `server` for the x64 size), with a `StandardSSD_LRS` OS
  disk of `osDiskSizeGB` and managed boot diagnostics on.
- Runs the bootstrap as a **CustomScript extension**: install Nix as a plain
  package manager, install the pinned `#runtime` profile from
  `agentBoxFlakeRef`, write `/etc/agent-box/config.yaml`, and
  `agentbox apply --first-boot`. The box **stays Ubuntu** — apt and
  unattended-upgrades keep owning the base OS; agent-box lives entirely in
  `/nix/var/nix/profiles/agent-box` and never calls apt after first boot.
- **Basic-auth-to-cookie web auth**, identical to the AWS path. The terminal
  lives at `/<userName>/` (default `/agent/`); Caddy prompts for the
  `userName` and the `webPassword`, sets an
  `HttpOnly; Secure; SameSite=Strict` cookie, then lets browser WebSocket
  upgrades authenticate with that cookie. ttyd binds only to localhost.
- Outputs `https://<addr>.sslip.io/<userName>/`, where `<addr>` is the static
  IPv4 with dots replaced by dashes. Only that dashed spelling is what
  `agentbox apply` derives and therefore the only one in the issued
  certificate — the dotted spelling resolves but fails TLS (issue #359).
- No port 80 is opened. Caddy is configured for TLS-ALPN-01 only.

## Three things that differ from `aws/lightsail-template.yaml`

This template is the Azure twin of the Lightsail one, not of the EC2 one:
same native Ubuntu + Nix path, same `agentbox apply`, same config. Three
things could not be carried across.

**The extension is the wait condition.** CloudFormation reports
CREATE_COMPLETE the moment the instance exists, so the AWS bootstrap ends by
signalling a `WaitCondition` handle with `agentbox signal`. An ARM deployment
does not complete until the VM extension reports its provisioning state, and a
non-zero exit from the script fails the deployment. So the handshake is gone,
and a first boot that goes wrong fails the deployment on its own.

**The web password is not readable from the API.** The Lightsail template's own
comment concedes that the plaintext password sits in the instance's launch
script, readable by anyone holding `lightsail:GetInstance`. Here the whole
bootstrap lives in the extension's `protectedSettings`, which ARM encrypts with
the VM's certificate and never returns — not from `az vm extension show`, not
from deployment history. `webPassword` is a `securestring`, so it is not
recorded in the deployment's parameters either.
`scripts/check_azure_template.py` fails the build if the script ever moves to
plain `settings`.

**A debug credential is mandatory.** Azure rejects a Linux `osProfile` with
neither an SSH public key nor an admin password, even with SSH closed at the
NSG. Hence `authenticationType` + `adminPasswordOrKey`, the pair every
azure-quickstart-templates VM sample uses. That credential is for
`adminUsername` (default `azureuser`) — the debug account. It is **not** the
agent user and **not** the browser terminal password.

## Sizes and cost

Azure has no Lightsail equivalent: no flat-rate bundle packaging compute, disk,
IP and transfer. The three pieces are billed separately, so the prices in the
size picker are the **compute half only**. A default box in westus3:

| Line item | Monthly |
| --- | --- |
| `Standard_B2pls_v2` (2 vCPU / 4 GiB, ARM Ampere) | $21.90 |
| Standard SSD E6, 64 GiB | $4.80 |
| Standard static IPv4 | $3.65 |
| **Total** | **~$30.35** |

For comparison the same shape on Lightsail is `medium_3_0` at $24/mo flat.

Two facts worth not re-deriving: **westus3 is the cheapest region for ARM**
($21.90 against $24.53 in eastus/westus2 and $28.03 in westeurope), and **ARM
Ampere is cheapest at every RAM tier** — which matches AWS, whose EC2 template
defaults to a Graviton `t4g.medium` of the same shape. The x64 size in the
picker is there for regions with no Ampere capacity, not because it is a
better deal.

**4 GiB is the floor**, higher than the AWS templates' 2 GiB, because Azure
offers nothing in between: `B2pts_v2` (1 GiB) is the only smaller 2-vCPU
Ampere size and it does not survive substituting the profile.

Unlike a Lightsail bundle the OS disk is resizable later: deallocate the VM,
`az disk update --size-gb`, start it, grow the filesystem.

## Deploying

From the portal, use the Deploy button above. From the CLI:

```bash
az group create -n agent-box -l westus3
az deployment group create -g agent-box \
  --template-file azure/agent-box.json \
  --parameters webPassword='<16-64 chars>' \
               adminPasswordOrKey="$(cat ~/.ssh/id_ed25519.pub)" \
               agent=claude
```

The deployment finishes when the box is actually configured — a few minutes:
installing Nix, substituting the profile, and Caddy issuing a Let's Encrypt
certificate against `<addr>.sslip.io`. There is no closure to build and no
reboot. Read the URL, the address and the Remote Control session name from
`properties.outputs`.

`webPassword` accepts 16-64 characters of `A-Z a-z 0-9 . _ ~ -` only. The
bootstrap passes it to the renderer through the environment and quotes it for
that set; anything outside it is not quoted for. (The AWS templates enforce
this with an `AllowedPattern`, which Bicep has no equivalent of — hence the
narrower charset than the AWS form's "any password-manager symbol".)

### If the deployment fails

The extension's exit code is the deployment's, so a failure means the
bootstrap failed. The reason is on the box, which is still running:

```bash
az vm extension show -g agent-box --vm-name agent-box-vm \
  -n agent-box-bootstrap --query "instanceView.substatuses[].message" -o tsv
```

On the box itself, `/var/log/agent-box-bootstrap.log` has the full trace (the
handler keeps its own copy of the same stream under
`/var/lib/waagent/custom-script/download/0/`). Boot diagnostics are on, so the
portal's serial console works even with `debugSsh=false`.

## Pinning

The AWS 1-click templates are published to S3 by `publish-template.yml`, which
injects the publishing commit into `AgentBoxFlakeRef` — so a 1-click AWS box
is reproducible. **The Azure button is not pinned.** It serves
`azure/agent-box.json` straight from `raw.githubusercontent.com` at `master`,
so a 1-click Azure box tracks the default branch, and pinning is a form field:
set `agentBoxFlakeRef` to `github:defangdevs/agent-box/<sha>`.

The button points at GitHub rather than at the S3 bucket the AWS templates use
because the Azure portal fetches the template from the browser and needs
`access-control-allow-origin`, which raw.githubusercontent.com sends and a
plain public-read S3 object does not. Publishing a pinned, CORS-enabled copy is
a follow-up, not a limitation of the template.

## Updating the template

1. Edit `agent-box.bicep`.
2. Recompile: `cd azure && az bicep build --file agent-box.bicep`.
3. `python3 scripts/check_azure_template.py` — recompiles and diffs, renders
   the bootstrap and parses it under `bash -n`, and checks the password stays
   in `protectedSettings`. This is what `azure-ci.yml` runs.
4. Optionally preflight against live Azure. `validate` creates nothing:
   ```bash
   az group create -n agent-box-validate -l westus3
   az deployment group validate -g agent-box-validate \
     --template-file azure/agent-box.json \
     --parameters webPassword=... adminPasswordOrKey=...
   az group delete -n agent-box-validate --yes
   ```
5. Commit the `.bicep` and the `.json` together.

The bootstrap script is a verbatim Bicep multi-line string with
`@@PLACEHOLDER@@` markers swapped in by `replace()`. It has to be: Bicep does
not interpolate inside `'''...'''`, which is exactly what a shell script full
of `${...}` needs. Add a parameter to the script and you must add its marker to
the `replace()` chain **and** to `SAMPLE` in the check script, which fails if
any marker survives substitution.

Keep the script ASCII. `az vm create` encodes `--custom-data` as latin-1 and
aborts the whole create on an em-dash; the same text passes through several
encoders here, and the check enforces it.

### A note on `.bicep-version`

The compiled JSON embeds the compiling CLI's version and a version-derived
`templateHash` in its `_generator` block, so two builds of identical source
differ cosmetically whenever the CLI does. `check_azure_template.py` strips
every `_generator` block before diffing, so you can compile with whatever
Bicep you have — nixpkgs' `bicep`, for instance, stamps a truncated `"0.39"`
where the official binary stamps the full version. `.bicep-version` still pins
what CI installs, so a genuine language-level change between releases shows up
as a diff instead of as noise. When you deliberately upgrade, bump the pin and
recompile in the same commit.

## Known gaps

- **IPv4 only.** The AWS templates are dual-stack and the EC2 one can run
  IPv6-only. Matching that on Azure needs a second IP configuration, an IPv6
  public IP and an IPv6 prefix on the vnet.
- **zram.** Azure's `linux-azure` kernel splits the `zram` module into a
  separate `linux-modules-extra` package that the marketplace images do not
  install, so `agent-box-zram.service` fails and the box quietly falls back to
  disk swap alone (issue #435). The bootstrap installs that package as a
  workaround; it is the Azure-image half of the bug, and whatever module-side
  fix #435 lands can drop it. The install is non-fatal on purpose — a box
  without compressed swap is degraded, not broken, and `earlyoom` still runs.
- **One user, one session.** Same as the AWS templates: add more from the
  box's own settings page or `agent-box-session add` afterwards.
