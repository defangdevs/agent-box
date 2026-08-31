# One spec, two backends (issue #451, PR 1).
#
# `bin/agentbox` reads a JSON/YAML config whose schema (`class Spec`,
# `bin/agentbox:272`) is annotated "same shape as the module's option"
# throughout — a THIRD hand-mirrored copy of the option tree, on top of the
# two renderers. Nothing checked the mirroring, and the two test configs that
# drove the two fixtures had drifted into describing different boxes: the
# golden config declares no sessions and no sudoAllowlist, the native one
# declares two sessions per user and an allowlist entry. Every cross-backend
# comparison of the two fixtures was therefore apples to oranges, which is why
# #356's twin-schema bugs (two seed producers, two tmpfiles builders) could
# only be found by hand.
#
# This function evaluates the MODULE's options into that JSON config, so the
# native fixture is rendered from the same box the golden fixture describes.
# `nix run .#update-native-config` writes the result to tests/native/config.json
# and the `one-spec-both-backends` check fails when the committed copy has
# drifted — so a new module option that the native schema cannot express is a
# red CI job, not a discovery on a live box.
#
# Deliberately NOT a general module→native translator. It is a test fixture
# generator: it maps what the golden configuration exercises. A field a native
# box gets from its own substrate rather than from config (the webhook script
# path, the profile's repo/rev manifest) is marked below and supplied here as
# the constant a native box would resolve.
{ lib }:
config:
let
  cfg = config.services.agent-box;

  # The module's per-session options, in the native config's spelling. Only
  # non-default values are emitted: the native Spec applies the SAME defaults
  # (bin/agentbox's user_seed, whose docstring says so), so emitting them
  # would hide a divergence in either default behind an explicit value.
  session = _: s:
    lib.filterAttrs (_: v: v != null) {
      inherit (s) resumePrompt remoteControlName;
      agent = s.agent;
      cwd = s.workingDirectory;
      prompt = s.initialPrompt;
    }
    // lib.optionalAttrs (!s.skipPermissions) { skipPermissions = false; }
    // lib.optionalAttrs (!s.remoteControl) { remoteControl = false; }
    // lib.optionalAttrs (s.extraArgs != [ ]) { inherit (s) extraArgs; };

  user = name: u:
    { }
    # The settings page / root vhost belongs to exactly one user. The module
    # names it box-side (web.user); the native schema flags it user-side.
    // lib.optionalAttrs (cfg.web.enable && cfg.web.user == name) { root = true; }
    // lib.optionalAttrs (u.agent != null && u.agent != cfg.agent) { inherit (u) agent; }
    // lib.optionalAttrs (u.environment != { }) { inherit (u) environment; }
    // lib.optionalAttrs (u.environmentFiles != [ ]) { inherit (u) environmentFiles; }
    // lib.optionalAttrs (u.seedMainSession != null) { inherit (u) seedMainSession; }
    // lib.optionalAttrs (u.sessions != { }) {
      sessions = lib.mapAttrs session u.sessions;
    };
in
{
  domain = cfg.web.domain;
  agents = cfg.installAgents;
  defaultAgent = cfg.agent;
  repo = cfg.selfUpdate.repo;
  rev = cfg.selfUpdate.rev;
  protectMemory = cfg.protectMemory;
  codexFullAccess = cfg.codexFullAccess;
  # Verbatim: these are operator-written sudoers command lines, and a fixture
  # generator has no business rewriting one. They ARE substrate-specific —
  # this configuration's entry names /run/current-system/sw/bin/systemctl,
  # which no native box has — so the cross-backend comparison normalizes the
  # systemctl prefix rather than the generator inventing a path. Worth knowing
  # while reading tests/native/expected/etc/sudoers.d: the NixOS-shaped line
  # there is this one, and it is also redundant, since the module grants the
  # caddy reload itself (caddyReloadCmd, modules/agent-box.nix.in:316) — which
  # is why the golden sudoers carries it twice.
  sudoAllowlist = cfg.sudoAllowlist;
  web = { enable = cfg.web.enable; }
    # Only when turned OFF, for the reason `session` above emits only
    # non-defaults: both backends default the reboot button on, and writing
    # that default out would hide a divergence in either default behind an
    # explicit value.
    // lib.optionalAttrs (!cfg.web.rebootButton) { rebootButton = false; };
  webhook = {
    enable = cfg.webhook.enable;
    repo = cfg.webhook.repo;
    hookSessionArgs = cfg.webhook.hookSessionArgs;
    # SUBSTRATE, not config: the module fetches local-channels and passes the
    # store path; `agentbox apply` installs the profile's pinned copy at this
    # fixed path and finds it there (bin/agentbox's profile_webhook_script
    # fallback). Naming it is what a native box's own config.yaml does.
    script = "/etc/agent-box/webhook.py";
  };
  users = lib.mapAttrs user cfg.users;
}
# The "@<host>" suffix of auto-derived Remote Control session names. Emitted
# only when the module's option is set, because an unset one now means the
# same thing on both backends: derive the label from the domain. (It did not
# always — remoteControlHost defaulted to config.networking.fqdnOrHostName,
# so a NixOS box named its sessions "agent-main@nixos" where an otherwise
# identical native box said "agent-main@golden.example.org". Reported by this
# check on its first run and fixed by making "" the module's default.)
// lib.optionalAttrs (cfg.remoteControlHost != "") {
  hostLabel = cfg.remoteControlHost;
}
# Where a lazily installed harness is fetched from (#416). The module reuses
# the pin its update service already maintains; a native box has no such pin,
# so its Spec defaults to the channel that pin tracks. Emitted only when the
# module actually HAS a pin — with agentNixpkgs null (this configuration, and
# any host that has not been through an update yet) the module resolves agent
# packages from its own pkgs instead, and there is no URL to hand over.
// lib.optionalAttrs (cfg.selfUpdate.agentNixpkgs != null) {
  jitNixpkgs = cfg.selfUpdate.agentNixpkgs.url;
}
