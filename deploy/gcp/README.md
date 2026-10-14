# Launch agent-box on Google Compute Engine

The GCP twin of [`../azure/`](../azure/README.md): stock Ubuntu 24.04, Nix as
a package manager, the pinned `#runtime` profile, `/etc/agent-box/config.yaml`
and `agentbox apply --first-boot`. Behavior stays in `modules/`, so a GCP box
behaves exactly like an Azure one.

GCP has no "Deploy to GCP" button (Deployment Manager is end-of-life, and an
"Open in Cloud Shell" link for a repo Google has not allow-listed opens
without the user's credentials). The launch path is a `gcloud` script run in
the user's own Cloud Shell.

## Deploy

```sh
git clone https://github.com/defangdevs/agent-box && cd agent-box
deploy/gcp/launch.sh --project MY_PROJECT --zone us-central1-a
```

Needs `gcloud`, `git` and `python3` (Cloud Shell has all three). Without
`--password-hash`, `launch.sh` generates a password, hashes it with argon2id
(Caddy's parameters) before anything leaves the machine, and prints it once.
It prints the URL, the sslip.io aliases and the exact teardown command.
`launch.sh` with a bad flag prints the options. Tear down with
`deploy/gcp/teardown.sh --project P --zone Z --name NAME`, which deletes everything and fails if
anything remains.

## What it creates

Every resource is named after `--name` (default `agent-box`):

- `<name>-net` (custom-mode VPC) and `<name>-subnet` on `10.42.0.0/24`.
- `<name>-https`: tcp/443 from `--allow-cidr` to the box's network tag.
- `<name>-iap-ssh`, only with `--debug-ssh`: tcp/22 from the IAP range
  `35.235.240.0/20`, with OS Login on and project SSH keys blocked.
- `<name>-ip`: a static regional IPv4, reserved before the VM exists.
- The VM: Ubuntu 24.04 LTS, 32 GB disk, Shielded VM, **no service account**.

## What differs from Azure

1. **No password in metadata.** Instance metadata is readable by every
   principal with `compute.instances.get`, and by every process on the box.
   The deployer hashes the password (argon2id) and the bootstrap passes only
   the hash to `agentbox apply` via `AGENT_BOX_WEB_PASSWORD_HASH`. Needs
   Caddy 2.11 or later.
2. **`startup-script` runs on every boot**, so the bootstrap exits at once if
   `/var/lib/agent-box/.gce-bootstrapped` exists. A failed first boot writes
   no marker and retries.
3. **Guest attributes are the wait condition.** The bootstrap writes
   `agent-box/status` (`running` ... `ready`, or `failed:<stage>:exit-<rc>`)
   and the launcher polls it.
4. **"Ready" means the URL works**: a verified TLS handshake to the IP,
   bounded at 5 minutes (`ready-tls-pending` if the certificate is late).
5. The launcher passes the reserved IP and the bootstrap checks it against
   the metadata server before Caddy requests a certificate.
6. No service account, so the default compute account's project-wide Editor
   role is never attached. Outbound tcp/25 is already blocked by GCE.

## Sizes and cost (us-central1, on-demand; check before relying on it)

| Line item | Monthly |
| --- | --- |
| `e2-medium` (2 shared vCPU, 4 GB) | ~$24.46 |
| 32 GB `pd-balanced` | ~$3.20 |
| Static IPv4, in use | ~$3.65 |
| **Total** | **~$31** |

Arm is not cheaper on GCP: `t2a-standard-1` costs more than `e2-medium`, and
T2A exists in only three regions. An unattached reserved IP costs double, so
teardown releases it.

## If the deployment fails

1. The guest attribute names the stage:
   `gcloud compute instances get-guest-attributes NAME --zone ZONE --query-path agent-box/`
2. `gcloud compute instances get-serial-port-output NAME --zone ZONE | grep startup-script`
3. `/var/log/agent-box-bootstrap.log` on the box (needs `--debug-ssh`).

## SSH

Off by default, like Azure's `debugSsh`. `--debug-ssh` opens tcp/22 to the
IAP range only; access is then IAM (`roles/compute.osLogin` plus
`roles/iap.tunnelResourceAccessor`).

## Checks

`scripts/check_gcp_template.py` (run by `.github/workflows/gcp-ci.yml`)
renders hostile values, checks no plaintext password, ASCII, `bash -n` and
shellcheck, feeds the hash to a real Caddy, runs the rendered script as root
in a bubblewrap sandbox with stubbed `curl`, `nix` and `apt-get`, and renders
the written config with the real `agentbox apply --root`. `live_test.sh` is
the manual end-to-end check against a real project; it always tears down.

## Known gaps

- Copy of the Azure bootstrap (Azure and GCP share about 94 of 106 code
  lines). Folding both into one is #530.
- No live CI deploy test yet, no launch-page entry, no Terraform module
  (phases 2 to 4 of #848).
- IPv6 is not configured.
