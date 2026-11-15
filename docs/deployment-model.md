# Deployment model: native Linux and NixOS

Status: accepted 2026-10-08

Agent-box supports two deployment backends from the same repository:

- a full NixOS system built from `modules/agent-box.nix`; and
- an ordinary Linux host with a pinned Nix `#runtime` system profile, an
  `/etc/agent-box/config.yaml` specification, and `agentbox apply` rendering
  native systemd configuration.

This note records why portable one-click cloud deployments use the native
backend by default while NixOS remains a supported deployment model.

## Decision

New portable cloud deployments should start from a provider-supported ordinary
Linux image and use the native backend unless a provider-specific reason makes
a NixOS appliance preferable. Ubuntu 24.04 is the current common base for AWS
Lightsail, Azure, and Hetzner.

NixOS is not deprecated. It remains the right backend for bare-metal NixOS
hosts, the qcow2 VM image, and appliance-style deployments such as the AWS EC2
template. The choice is a product default, not a statement that one backend is
capable and the other is not.

Both backends must continue to derive behavior from the shared sources under
`modules/`. Provider templates provision infrastructure, transport first-boot
input, and report completion; they must not grow a separate agent-box policy.

## Why the native backend became the cloud default

### Lightsail was the forcing function

AWS Lightsail offers provider blueprints rather than the generally
distributable custom-image model available through EC2 AMIs. A prepared
Lightsail instance can be snapshotted and launched again, but those snapshots
belong to one Lightsail account. They can accelerate repeated launches for
that account; they cannot be the image behind a public one-click template used
in arbitrary customer accounts.

An earlier deployment converted a stock Lightsail instance to NixOS during
first boot. That made provisioning responsible for replacing the operating
system and introduced another failure and recovery mode. The native backend
instead keeps the supported Ubuntu blueprint, installs Nix as a package
manager, installs the pinned agent-box runtime, and applies the shared
specification without a conversion or reboot.

Once that backend existed, using the same contract on Azure and Hetzner kept
the provider-specific layer small:

| Stage | Lightsail | Azure | Hetzner |
| --- | --- | --- | --- |
| Base OS | Ubuntu 24.04 | Ubuntu 24.04 | Ubuntu 24.04 |
| Software | pinned `#runtime` profile | pinned `#runtime` profile | pinned `#runtime` profile |
| Declared state | `/etc/agent-box/config.yaml` | `/etc/agent-box/config.yaml` | `/etc/agent-box/config.yaml` |
| Activation | `agentbox apply --first-boot` | `agentbox apply --first-boot` | `agentbox apply --first-boot` |
| Completion | CloudFormation wait condition | VM extension status | HTTPS readiness poll |

### Ordinary Linux is the compatibility boundary agents expect

Coding agents operate arbitrary development tools, not only software selected
by the agent-box maintainers. Their generated commands, vendor installers,
language managers, Docker instructions, and troubleshooting guidance commonly
assume an FHS-style system with paths such as `/usr/bin` and `/etc`, a familiar
dynamic linker, `apt`, and conventional systemd administration.

NixOS can run those tools, but software that assumes an ordinary distribution
often needs wrappers, patched binaries, an FHS environment, or Nix-specific
instructions. Every compatibility shim expands the platform surface that
agent-box must maintain.

The native backend deliberately draws the boundary between the host and the
control plane:

- Ubuntu owns the kernel, base operating system, provider agents, and security
  updates.
- Nix owns the pinned agent-box runtime and its tool closure.
- `agentbox apply` owns the users, systemd units, sudoers rules, Caddy
  configuration, and other agent-box policy.

This preserves reproducibility where agent-box needs it while leaving the
workspace recognizable to agents and third-party tooling.

### Provider-supported images reduce operational surface

Using a stock image retains the provider's expected boot integration, serial
console and recovery behavior, cloud agent, and patching path. A full NixOS
default would require a maintained image or conversion path for every provider,
architecture, region, and image lifecycle.

That cost can be justified for an immutable appliance. It is not required to
deliver the default coding-agent workstation.

## What NixOS still does better

NixOS provides a single declarative operating-system closure, atomic
whole-system generations, and less duplication between a distribution
userland and the Nix store. It is a strong choice when appliance
reproducibility and system-level rollback matter more than compatibility with
arbitrary developer tooling or provider-native images.

The native backend has explicit costs:

- Ubuntu and the Nix runtime duplicate parts of the userland.
- Base-OS updates and agent-box updates use separate mechanisms.
- A Nix profile rollback does not roll back the kernel or Ubuntu packages.
- The resulting root filesystem is larger.

Those are accepted consequences of the default, not deficiencies to hide in
provider templates.

## Measurements from the Hetzner implementations

The NixOS experiment in PR #839 and native implementation in PR #842 were
deployed on the same Hetzner `cx23` shape: 2 vCPU, 4 GiB RAM, and a 40 GiB
root disk. Measurements taken on 2026-10-08 were:

| Metric | NixOS with `nixos-anywhere` | Ubuntu with native systemd |
| --- | ---: | ---: |
| Root filesystem used after deployment | about 2.3 GiB | about 5.8 GiB |
| Used excluding native backend's 2 GiB swap file | about 2.3 GiB | about 3.8 GiB |
| Relevant Nix closure | about 1.8 GiB full system | 733 MiB runtime |
| Deployment invocation to authenticated HTTPS | about 3m15s | about 3m15s |

These are engineering observations, not universal benchmarks. The NixOS
installation reused locally built artifacts from earlier attempts; the native
installation started from a clean stock image and used binary substitutes.
The timing measures the successful deployment invocation through an
authenticated HTTPS response, not kernel-only boot time.

The results still establish two useful points:

1. NixOS was smaller on disk.
2. The unbaked first-ready times were effectively tied.

Ubuntu is therefore the default for interoperability and operational
consistency, not because it is inherently smaller or faster.

## Baked images are an optimization, not configuration

A native deployment may start from a trusted image with Nix and the exact
pinned runtime profile already installed. First boot must still provide the
instance specification and run `agentbox apply --first-boot`.

The image must not contain instance identity or mutable state, including:

- `/etc/machine-id` or SSH host keys;
- authorized keys that are not intentionally shared by every instance;
- web password hashes, cookie secrets, Caddy accounts or certificates, or a
  public hostname;
- cloud credentials, agent logins, tokens, webhook secrets, or sessions; and
- a pre-created swap file when it can be created cheaply on first boot.

The stock-image path remains the image-builder and recovery path. A stale or
incorrect baked image must fail its runtime-profile verification rather than
silently installing a different release.

## Rules for future providers

1. Prefer the native backend on a provider-supported ordinary Linux image.
2. Reuse the pinned runtime, configuration schema, and `agentbox apply`; do
   not reimplement agent-box behavior in cloud-init or a provider template.
3. Keep provider code to resource provisioning, network integration,
   first-boot transport, and a truthful completion signal.
4. Treat a NixOS image as an optional appliance path, not a prerequisite for
   supporting the provider.
5. Treat baked images as replaceable caches. Keep secrets, identity,
   certificates, and user state in per-instance first boot.
6. Test backend parity when behavior changes. A feature present on only one
   backend is either an intentional documented difference or a bug.

## When to revisit this decision

Reconsider the default if ordinary-Linux compatibility stops being important,
the product becomes a closed appliance rather than a general coding
workstation, or every supported provider gains a maintainable and
distributable NixOS image path whose lifecycle is cheaper than the native
backend. Faster first boot alone is not enough; a baked native runtime can
provide that optimization without changing the host contract.

## History

- [Issue #154](https://github.com/defangdevs/agent-box/issues/154) introduced
  the native backend and parity work.
- [Issue #390](https://github.com/defangdevs/agent-box/issues/390) tracks the
  Lightsail move away from the earlier NixOS conversion path.
- [Issue #7](https://github.com/defangdevs/agent-box/issues/7) is the Hetzner
  deployment issue, revised from `nixos-anywhere` to native systemd.
- [PR #839](https://github.com/defangdevs/agent-box/pull/839) preserves the
  successful NixOS-on-Hetzner experiment.
- [PR #842](https://github.com/defangdevs/agent-box/pull/842) implements the
  native Ubuntu Hetzner deployment.
