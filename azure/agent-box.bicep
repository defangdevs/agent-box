// agent-box on Azure - the Bicep twin of aws/lightsail-template.yaml.
//
// One Ubuntu 24.04 VM that STAYS Ubuntu: Nix is installed as a plain package
// manager, the pinned runtime profile brings the agent CLIs, tmux, ttyd and
// Caddy, and `agentbox apply` renders the users, units, sudoers, Caddyfile and
// first-boot secrets. No image build, no reboot.
//
// Canonical source. Compile to agent-box.json with:
//   az bicep build --file agent-box.bicep
// CI verifies the committed JSON matches; do not edit the JSON by hand. The
// JSON is what the README's "Deploy to Azure" button hands the portal.
//
// Three deliberate differences from the AWS template, all forced or enabled by
// the platform:
//
//   1. No wait-condition handshake. CloudFormation reports CREATE_COMPLETE the
//      moment the instance exists, so the AWS bootstrap ends with
//      `agentbox signal <handle>` against a WaitCondition. An ARM deployment
//      does not finish until the VM extension reports its provisioning state,
//      and a non-zero exit from the script fails the deployment. The extension
//      IS the wait condition, so the handshake is gone.
//   2. The web password is not world-readable. The Lightsail launch script
//      carries the plaintext password to anyone holding lightsail:GetInstance.
//      Here the whole bootstrap lives in the extension's `protectedSettings`,
//      which ARM encrypts with the VM's own certificate and never returns from
//      the API - not from `az vm extension show`, not from deployment history.
//   3. A credential is mandatory. Azure rejects a Linux `osProfile` carrying
//      neither an SSH key nor an admin password, even with SSH closed at the
//      NSG. Hence the authenticationType / adminPasswordOrKey pair, the same
//      shape every azure-quickstart-templates VM sample uses.

@description('Linux user that owns the agent sessions, and the login name for the browser terminal.')
@minLength(1)
@maxLength(32)
param userName string = 'agent'

@description('Password for the browser terminal, 16-64 characters. Any character is safe: it reaches the bootstrap base64-encoded, so nothing in it can break out of the shell literal it lands in. Hashed with argon2id on first boot, so the plaintext never lands on the box\'s disk, and it travels in the extension\'s encrypted protectedSettings rather than in readable instance metadata. Change it later from the box\'s own settings page.')
@secure()
@minLength(16)
@maxLength(64)
param webPassword string

@description('Which agent CLI the box\'s initial session runs.')
@allowed([
  'claude'
  'codex'
])
param agent string = 'claude'

// The picker annotates each value with vCPU/RAM/price because the portal's
// generated form renders allowed values verbatim - there is no separate label
// field, exactly as with the Lightsail template's BundleId. Only the first
// token reaches Azure; everything from the first space on is decoration.
//
// Azure has no Lightsail-style bundle: compute, disk and the public IPv4 are
// three separate line items, so the prices below are the COMPUTE half only
// (westus3, the cheapest region for Ampere). Add ~$4.80/mo for the default
// 64 GiB Standard SSD and ~$3.65/mo for the static IPv4.
//
// 4 GiB is the floor here rather than the 2 GiB the AWS template allows: the
// B2pts_v2 (1 GiB) and B2pls_v2 (4 GiB) are the only two 2-vCPU Ampere sizes,
// with nothing in between, and 1 GiB does not survive substituting the
// profile.
@description('VM size (the id before the parenthesis; specs and compute-only westus3 price shown for convenience). ARM Ampere is cheaper than x64 at every RAM tier - the x64 size is here for regions with no Ampere capacity.')
@allowed([
  'Standard_B2pls_v2 (2 vCPU / 4 GiB / ARM Ampere / ~$21.90 mo)'
  'Standard_B2ps_v2 (2 vCPU / 8 GiB / ARM Ampere / ~$30.66 mo)'
  'Standard_B4pls_v2 (4 vCPU / 8 GiB / ARM Ampere / ~$43.80 mo)'
  'Standard_B4ps_v2 (4 vCPU / 16 GiB / ARM Ampere / ~$61.32 mo)'
  'Standard_B2als_v2 (2 vCPU / 4 GiB / x64 AMD / ~$24.82 mo)'
])
param vmSizeChoice string = 'Standard_B2pls_v2 (2 vCPU / 4 GiB / ARM Ampere / ~$21.90 mo)'

@description('OS disk size in GiB. Unlike a Lightsail bundle this is resizable later (stop the VM, grow the disk, grow the filesystem) - so start small.')
@minValue(30)
@maxValue(2048)
param osDiskSizeGB int = 64

@description('Deployment-specific instructions APPENDED to the box\'s AGENTS.md (the agent\'s standing instructions), below the built-in platform guide. Empty = just that guide.')
param agentsMd string = '''
## This box (Azure)

- This is an Azure Linux VM running Ubuntu with agent-box installed through
  Nix. The OS disk persists but RAM does not: a reboot or a
  deallocate/start loses the live tmux session, so save working context to
  disk under your home. The public IP is a Standard SKU static address, so
  the public address (and your URL) survives a stop/start.
- Your URL is derived from that static IP via sslip.io. It is stable, but
  always read $AGENT_BOX_URL rather than hard-coding it.
- The base OS is ordinary Ubuntu: apt and unattended-upgrades own it, and
  the Azure portal's serial console works (boot diagnostics are on).
  agent-box itself never calls apt - it lives in the Nix profile
  /nix/var/nix/profiles/agent-box.
- Low on disk? The OS disk is resizable: deallocate the VM, grow the disk in
  the portal or with `az disk update --size-gb`, start it, then grow the
  filesystem. Scheduled nix garbage collection reclaims store space in the
  meantime.
'''

@description('Flake reference the runtime profile is installed from. Pin it to a revision (github:defangdevs/agent-box/<sha>) for a reproducible box; the bare ref tracks the default branch. The AWS 1-click templates get this pinned at publish time; the Azure button serves the committed template straight from GitHub, so pinning here is yours to do.')
param agentBoxFlakeRef string = 'github:defangdevs/agent-box'

@description('Nix installer. The Determinate installer is used deliberately: it supports SELinux (so the same script serves RHEL), survives distro upgrades, and enables flakes out of the box.')
param nixInstallerUrl string = 'https://install.determinate.systems/nix'

@description('Source range allowed to reach the terminal (and SSH). A CIDR, or an Azure service tag such as Internet.')
param allowCidr string = '0.0.0.0/0'

// Default false, where the AWS templates default their DebugSsh to true. Not
// caution for its own sake: on Lightsail, SSH is the only way to read
// /var/log/agent-box-bootstrap.log after a first boot that went wrong, so the
// port has to be open by default for the box to be debuggable at all. Azure
// has managed boot diagnostics, so the serial console reaches the same box
// authenticated through Azure RBAC instead of through a credential on an open
// port - strictly better, and it works with 22 shut. Leaving 22 open by
// default would also mean a portal user who picks authenticationType=password
// for convenience gets SSH password auth exposed to allowCidr, which defaults
// to the whole internet.
@description('Open port 22 to allowCidr. Off by default: the portal\'s serial console already gives debug access, authenticated through Azure RBAC rather than through a credential on an open port. Turn it on for an ordinary SSH session - the box is ordinary Ubuntu, and nothing here wipes /home or /root.')
param debugSsh bool = false

@description('Linux user for SSH/serial-console debug access. NOT the agent user - the agent runs as userName.')
param adminUsername string = 'azureuser'

@description('Whether adminPasswordOrKey below is an SSH public key or a password. Azure will not create a Linux VM with neither, even when SSH is closed at the NSG.')
@allowed([
  'sshPublicKey'
  'password'
])
param authenticationType string = 'sshPublicKey'

@description('SSH public key (ssh-ed25519 AAAA...) or admin password for adminUsername, per authenticationType. This is the debug credential, not the browser terminal password.')
@secure()
param adminPasswordOrKey string

@description('Prefix for every resource name in the group.')
@minLength(1)
@maxLength(40)
param namePrefix string = 'agent-box'

@description('Azure region. Defaults to the resource group\'s.')
param location string = resourceGroup().location

// ---------------------------------------------------------------------------

var vmSize = split(vmSizeChoice, ' ')[0]

// The image SKU is looked up from an explicit map rather than sniffed out of
// the size string. Ampere sizes are spelled with a `p` in the family token
// (B2pls_v2) and x64 ones are not, but that is a naming convention, not a
// contract - and booting the arm64 image on an x64 size fails at allocation
// with nothing pointing at the cause. Adding a size to the picker above means
// adding its arch here, which is the point.
var imageSkuBySize = {
  Standard_B2pls_v2: 'server-arm64'
  Standard_B2ps_v2: 'server-arm64'
  Standard_B4pls_v2: 'server-arm64'
  Standard_B4ps_v2: 'server-arm64'
  Standard_B2als_v2: 'server'
}

// Both arms are spelled out rather than leaving the password one null, so the
// consequence of picking password auth is visible in the source instead of
// being inferred from an absent property: choosing it DOES enable SSH password
// authentication, to whatever allowCidr allows, whenever debugSsh is on.
var linuxConfiguration = authenticationType == 'password' ? {
  disablePasswordAuthentication: false
} : {
  disablePasswordAuthentication: true
  ssh: {
    publicKeys: [
      {
        path: '/home/${adminUsername}/.ssh/authorized_keys'
        keyData: adminPasswordOrKey
      }
    ]
  }
}

var sshRule = debugSsh ? [
  {
    name: 'ssh'
    properties: {
      priority: 1001
      protocol: 'Tcp'
      access: 'Allow'
      direction: 'Inbound'
      sourceAddressPrefix: allowCidr
      sourcePortRange: '*'
      destinationAddressPrefix: '*'
      destinationPortRange: '22'
    }
  }
] : []

// Port 80 is deliberately absent: Caddy is configured for TLS-ALPN-01 only, so
// nothing on the box ever answers there.
var httpsRule = {
  name: 'https'
  properties: {
    priority: 1000
    protocol: 'Tcp'
    access: 'Allow'
    direction: 'Inbound'
    sourceAddressPrefix: allowCidr
    sourcePortRange: '*'
    destinationAddressPrefix: '*'
    destinationPortRange: '443'
  }
}

// Verbatim, because Bicep does not interpolate inside a multi-line string -
// which is exactly what we want for a shell script full of ${...}. Parameters
// go in through replace() below, the same way the Lightsail template seds its
// own placeholders. Keep this ASCII: it is base64'd into the template, read
// back by the extension handler, and the AWS/Azure CLIs have each been seen to
// mangle a non-ASCII byte on the way through (az vm create encodes
// --custom-data as latin-1 and dies on an em-dash).
var bootstrapTemplate = '''
#!/usr/bin/env bash
# agent-box native bootstrap, run once as root by the CustomScript extension.
#
# The handler decodes protectedSettings.script to a file and executes it, so
# the shebang above is honoured - but the guard below costs nothing and keeps
# the dialect independent of what any future handler does with it. The AWS
# twin of this script needs the guard for real: Lightsail PREPENDS its own
# "#!/bin/sh" preamble, which ran the whole thing under dash and killed every
# launch the old template ever had (issue #390).
if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi
set -euxo pipefail
exec > >(tee /var/log/agent-box-bootstrap.log) 2>&1

# The extension handler runs with an almost empty environment - no HOME. Nix's
# own profile script dereferences $HOME unguarded (NIX_LINK=$HOME/.nix-profile),
# so sourcing it under `set -u` aborts the bootstrap with "HOME: unbound
# variable". Everything after this line wants a HOME too: `nix profile add`
# writes ~/.cache/nix, and the agent CLIs read ~/.config.
export HOME="${HOME:-/root}"
export DEBIAN_FRONTEND=noninteractive

# CustomScript can start while cloud-init is still running its own apt phase,
# and two apt-get runs fight over the dpkg lock. Wait it out, then still take
# the lock with a timeout rather than failing the deployment on a race.
cloud-init status --wait || true
APT="apt-get -o DPkg::Lock::Timeout=600 -qq"

# zram. Azure's linux-azure kernel splits the zram module into a separate
# linux-modules-extra package that the Ubuntu marketplace images do not
# install, so agent-box-zram.service fails on every Azure box and the box
# silently drops to disk swap alone (issue #435). This is the Azure-image half
# of that bug; whatever module-side fix #435 lands can drop these two lines.
# Non-fatal: a box without compressed swap is degraded, not broken - earlyoom
# still runs - and a kernel with no matching package must not fail the deploy.
$APT update || true
$APT install -y "linux-modules-extra-$(uname -r)" || \
  echo "WARNING: linux-modules-extra unavailable; zram will be missing (issue #435)"

# Swap. There is no closure build on this path, but substituting the profile
# peaks high enough to be worth carrying, and the agent's own work leans on it.
if ! swapon --show=NAME --noheadings | grep -qx /agent-box-swap; then
  fallocate -l 2G /agent-box-swap || \
    dd if=/dev/zero of=/agent-box-swap bs=1M count=2048
  chmod 600 /agent-box-swap
  mkswap /agent-box-swap
  swapon /agent-box-swap
  echo '/agent-box-swap none swap sw 0 0' >> /etc/fstab
fi

# Nix as a package manager. Not NixOS: this box stays Ubuntu, so apt keeps
# owning the base OS (and unattended-upgrades keeps patching it).
# `agentbox apply` never calls apt.
curl -fsSL "@@NIXINSTALLER@@" | sh -s -- install linux --no-confirm --determinate
# `set +u` around a script we do not own: the installer's profile snippet is
# free to reference whatever the next release wants, and it must not be able to
# abort the bootstrap.
set +u
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
set -u

# The runtime profile: payloads, tools, agent CLIs, shared assets. A system
# profile, so `nix profile rollback --profile ...` is the undo button and no
# user's own profile is touched.
nix profile install \
  --profile /nix/var/nix/profiles/agent-box \
  "@@FLAKEREF@@#runtime"
AGENTBOX=/nix/var/nix/profiles/agent-box/bin/agentbox

# The box's declared state. Everything host-specific the renderer needs, and
# nothing it can discover for itself: `domain: auto` tells --first-boot to
# settle the public IPv4 and derive the sslip.io hostname from it. That works
# unchanged on Azure because settle_public_ip asks checkip.amazonaws.com rather
# than a cloud's own metadata service.
install -d -m 0755 /etc/agent-box
cat > /etc/agent-box/config.yaml <<'AGENTBOX_CONFIG'
domain: auto
agents: [claude, codex]
defaultAgent: @@AGENT@@
web:
  enable: true
users:
  @@USER@@:
    root: true
    sessions:
      main:
        agent: @@AGENT@@
AGENTBOX_CONFIG

# Extra standing instructions for the agent, if the deployment gave any.
install -d -m 0755 /etc/agent-box-guides
cat > /etc/agent-box-guides/AGENTS.stack.md <<'AGENTBOX_AGENTSMD'
@@AGENTSMD@@
AGENTBOX_AGENTSMD

# Render and start everything. The password reaches the renderer through the
# environment and is hashed with argon2id, so it is never written to the box's
# disk in the clear.
#
# It arrives here BASE64-ENCODED, and that is load-bearing rather than tidy.
# Bicep has no pattern constraint for a parameter - only minLength/maxLength -
# so nothing can reject a password containing a single quote, and a raw
# substitution into the literal below would let one close the quote and run the
# rest of the password as root during first boot. Base64's alphabet is
# A-Za-z0-9+/= , so the encoded form cannot break out of any quoting.
#
# set +x for this one command: the trace this script runs under writes every
# expanded word to /var/log/agent-box-bootstrap.log, so leaving it on would put
# the password - encoded, which is not a secret-keeping measure - in a readable
# log on the box, which is precisely what "never written to disk" is supposed to
# mean. The extension also copies our stdout into
# /var/lib/waagent/custom-script/download/0/stdout.
set +x
AGENT_BOX_WEB_PASSWORD="$(printf %s '@@WEBPASSWORD@@' | base64 -d)" \
  "$AGENTBOX" apply --first-boot --settle-delay 60
set -x

# No signal step. This script's exit code is the deployment's: a failed apply
# exits non-zero above (set -e), the extension goes to Failed, and the ARM
# deployment fails instead of reporting a box that does not work.
echo "agent-box bootstrap complete"
'''

var bootstrap = replace(replace(replace(replace(replace(replace(
  bootstrapTemplate,
  '@@NIXINSTALLER@@', nixInstallerUrl),
  '@@FLAKEREF@@', agentBoxFlakeRef),
  '@@AGENT@@', agent),
  '@@USER@@', userName),
  '@@AGENTSMD@@', agentsMd),
  // base64, not the plaintext: see the comment above the apply, and
  // check_secrets() in scripts/check_azure_template.py, which fails if this
  // ever goes back to a raw substitution.
  '@@WEBPASSWORD@@', base64(webPassword))

// ---------------------------------------------------------------------------

// Static, so the derived sslip.io hostname keeps resolving across a
// stop/start. A Standard-SKU public IP has no dynamic option anyway.
resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${namePrefix}-pip'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${namePrefix}-nsg'
  location: location
  properties: {
    securityRules: concat([httpsRule], sshRule)
  }
}

// 10.42.0.0/16 to match the AWS template's VPC, so the two deployments are
// diffable side by side.
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${namePrefix}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'default'
        properties: {
          addressPrefix: '10.42.0.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${namePrefix}-nic'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: vnet.properties.subnets[0].id
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: '${namePrefix}-vm'
  location: location
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: imageSkuBySize[vmSize]
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: osDiskSizeGB
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
    osProfile: {
      computerName: namePrefix
      adminUsername: adminUsername
      adminPassword: authenticationType == 'password' ? adminPasswordOrKey : null
      linuxConfiguration: linuxConfiguration
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    // Managed boot diagnostics, so the portal's serial console and boot
    // screenshot work without a storage account of our own. That is the only
    // way in if the bootstrap wedges before Caddy is up and SSH is closed.
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

// The deployment's wait condition, and the only place the secrets live.
resource bootstrapExtension 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'agent-box-bootstrap'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.Extensions'
    type: 'CustomScript'
    typeHandlerVersion: '2.1'
    autoUpgradeMinorVersion: true
    protectedSettings: {
      script: base64(bootstrap)
    }
  }
}

// ---------------------------------------------------------------------------

// sslip.io resolves both 4.236.84.197.sslip.io and 4-236-84-197.sslip.io, but
// only the dashed spelling is what `agentbox apply` derives and therefore the
// only one in the issued certificate - the dotted one fails TLS rather than
// simply not existing (issue #359).
var host = '${replace(publicIp.properties.ipAddress, '.', '-')}.sslip.io'

@description('Browser terminal. Sign in with the userName and the webPassword chosen at deployment time. The first load waits on Caddy\'s ACME certificate. (The URL deliberately carries no user@ prefix: Chrome answers the auth challenge with URL userinfo plus an EMPTY password, and credentials typed into the prompt cannot override the URL-embedded identity.)')
output webUrl string = 'https://${host}/${userName}/'

@description('Claude Remote Control session name. After finishing `claude login` once in the browser terminal, the Claude desktop and mobile apps can drive this session. Only meaningful when agent=claude.')
output remoteControlSession string = '${userName}-main@${host}'

@description('Static public IPv4 address.')
output publicAddress string = publicIp.properties.ipAddress

@description('Debug access. Closed at the NSG unless debugSsh is true; the portal\'s serial console works either way.')
output sshCommand string = 'ssh ${adminUsername}@${publicIp.properties.ipAddress}'

@description('Where first-boot output lands if the deployment fails. The extension handler keeps its own copy of the same stream under /var/lib/waagent/custom-script/download/0/.')
output bootstrapLog string = '/var/log/agent-box-bootstrap.log'
