# agent-box-harness — what this box installed into your profile, and how to
# move it (issues #559, #590, #614).
#
# Those three reports are one report: a model or a feature needs a newer
# CLI, the box updates, and the CLI does not move. It cannot. A harness is
# installed just-in-time into the USER's profile, once, when the binary is
# missing (issue #416) — so a release that bumps the pin reaches a box that
# has never started that harness, and no other. Both "bump nixpkgs" PRs
# that answered those reports were therefore no-ops on any running box.
#
# The missing half was never a pin. It was a verb. This is the verb, and
# the box's own update runs it; nothing here is reserved to the update,
# because a person who wants their CLI moved now should not have to wait
# for a release. What it deliberately does NOT do is touch the rest of
# your profile: agent-box put claude, codex and defang there, and the
# packages you added yourself are yours. `nix profile upgrade --all` is
# still one command away, and this prints it.
@@include:lib/agents.sh@@

_hc_usage() {
  cat >&2 <<'USAGE'
usage: agent-box-harness ls
       agent-box-harness upgrade [NAME...]

  ls        what this box installed for you, and the pin it came from
  upgrade   move those onto the pin this box carries now; no NAME means
            every harness this box installed for you

A harness is the CLI PROGRAM a session runs (claude, codex) — not an agent
profile, which is a harness plus a model and an effort level. `agent-box-
profile` is the other one.

The pin moves when the box updates, so an upgrade right after one is the
run that has something to do. Sessions already running keep the binary
they started with until they restart.
USAGE
}

_hc_ls() {
  _hc_any=
  for _hc_n in $(agent_names); do
    _hc_bin="$HOME/.nix-profile/bin/$_hc_n"
    [ -x "$_hc_bin" ] || continue
    _hc_any=1
    _hc_pin_f="$(_jit_pin_file "$_hc_n")"
    _hc_pin="(unrecorded — installed before this box could write it down)"
    [ -r "$_hc_pin_f" ] && read -r _hc_pin < "$_hc_pin_f"
    printf '%s\n  installed: %s\n  pin:       %s\n' \
      "$_hc_n" "$_hc_bin" "$_hc_pin"
    if [ "$_hc_pin" = "${AGENT_BOX_NIXPKGS:-}" ]; then
      printf '  status:    current\n'
    else
      printf '  status:    the box now pins %s — `agent-box-harness upgrade %s`\n' \
        "${AGENT_BOX_NIXPKGS:-(unset)}" "$_hc_n"
    fi
  done
  [ -n "$_hc_any" ] || echo "no harness installed for $(id -un) yet — a" \
    "session that names one fetches it" >&2
}

_hc_main() {
  case "${1:-}" in
    (ls|list) shift; [ $# -eq 0 ] || { _hc_usage; return 2; }; _hc_ls ;;
    (upgrade)
      shift
      # No NAME means every harness this box installs. Word splitting is
      # the point here: agent_names prints one per line and none of them
      # can contain a space (agent_attr is a closed set of two literals).
      # shellcheck disable=SC2046
      [ $# -gt 0 ] || set -- $(agent_names)
      _hc_rc=0
      for _hc_n in "$@"; do
        agent_upgrade "$_hc_n" || _hc_rc=1
      done
      # Said once, at the end, and never by agent_upgrade itself: the
      # narrow default is a boundary, not a refusal, and a reader who is
      # told where the boundary is does not have to guess whether the
      # command silently skipped something (see the note at the top).
      echo "" >&2
      echo "This moved only what agent-box installed. For the rest of" \
           "your own profile: nix profile upgrade --all" >&2
      return $_hc_rc
      ;;
    (""|-h|--help|help) _hc_usage; return 0 ;;
    (*) echo "agent-box-harness: unknown command '$1'" >&2; _hc_usage; return 2 ;;
  esac
}

_hc_main "$@"
