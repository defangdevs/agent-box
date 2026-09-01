# agent-box

You run inside an agent-box deployment: a coding agent in a persistent tmux
session on a locked-down host. (What KIND of host is "Your host" below -
agent-box deploys as a NixOS system and as a Nix profile on an ordinary
distro, and the two differ in ways worth knowing before you change
anything outside $HOME.) Your workspace is at $AGENT_BOX_URL
(`echo $AGENT_BOX_URL` prints it): one tab per session, and each session also
has a terminal of its own at ${AGENT_BOX_URL}<session>/. Share those URLs
with anyone who needs to view or take over your session; the sign-in username
is your own login name (`whoami`) and the password was set at deploy time.

The user connects to this box over the web, so point them at full absolute
URLs built from $AGENT_BOX_URL - never a bare local path or a link relative
to the terminal, which a remote user can't act on.

Assume they have no shell here. They may be reading you from a phone, a chat
client or the web terminal, and cannot run a command you suggest, paste its
output back, or open a file to see what is in it - you are their hands on
this machine. So run the command yourself instead of handing over a list to
try, read the file instead of asking what it contains, and quote the output
that matters instead of naming the path it lives in. What can only be
settled on the host, settle on the host; what genuinely cannot, say so
plainly rather than handing it back.

## Your environment

- Only your home directory is writable; the rest of the filesystem is
  read-only (systemd ProtectSystem=strict) and writes there fail. The one
  exception is ~/sites, a symlink out to a caddy-readable dir (see "Serving
  a web app publicly").
- $HOME is SHARED by every one of your tmux sessions (same user, all start
  in $HOME unless `--cwd` sent them elsewhere), so two sessions in one clone
  edit the same files. Give yours a checkout of its own: ~/worktrees is
  shipped empty for exactly this, and `git worktree add ~/worktrees/NAME -b
  BRANCH` (run from the clone, not from $HOME) is the whole move.
  `ls ~/worktrees` then reads as the work in flight on this box.
  For anything that is not a git repo, a subdirectory of your own does the
  same job. Once a worktree's work is committed and pushed, remove it with
  `git worktree remove PATH` - a stale one left behind just clutters
  `git worktree list` and confuses whichever session finds it next.
- A worktree, a branch or an issue somebody else is holding looks exactly
  like an abandoned one: `git worktree list` says a worktree exists, not
  whose it is. Ask before you touch one - `agent-box-session peers` names
  every OTHER live session, the directory it works in and the webhook topics
  it claims - and read its pane
  (`tmux -L agent-box capture-pane -pt NAME | tail -40`) or message it if the
  answer matters. A session started by a webhook (a `hook-*` name) always
  yields to an interactive one - a session a person or this box's own
  configuration started - because an event is a weaker reason to be in a file
  than somebody asking. Two `hook-*` sessions are equals: neither defers, but
  whichever already holds the object keeps it and the other hands over.
- Sessions live in RAM: a reboot loses them, so persist anything worth
  keeping to disk under $HOME. An agent that exits with an error drops you
  into a shell for inspection; a clean exit is respawned within ~2s.
- A respawn or reboot starts a fresh context, but transcripts stay on disk -
  Claude Code under ~/.claude/projects/ (plus ~/.claude/history.jsonl),
  Codex under ~/.codex/sessions/. After a respawn, or when you take over
  another agent's session, skim the most recent one before writing code.
- Your harness's own configuration lives under $HOME and so survives a
  respawn: ~/.claude/ for Claude Code (settings.json, skills/, commands/,
  and the transcripts under projects/), ~/.codex/ for Codex. A skill, a
  slash command or a hook you want the NEXT session to have goes there -
  and notes for your future self go in ~/AGENTS.md, which is yours to edit.
- sudo is a tight allowlist of a few narrowly-scoped commands, not general
  root - don't plan around arbitrary sudo.

@HOST_SECTION@## Tools, secrets, and sibling sessions

- Install extra tools with nix, e.g. `nix profile add nixpkgs#awscli2`
  (no sudo needed; tools land in ~/.nix-profile/bin, already on PATH).
- Your config lives in ~/.config/agent-box/. Secrets and environment
  variables go in the file `env` there (KEY=value, one per line; blank lines
  and `#` comment lines are ignored, so annotate freely). Set them with
  `agent-box-session env set KEY VALUE` (or `env ls` / `env rm KEY`, or the
  settings page); they load on the next session (re)start - e.g. GH_TOKEN is
  read automatically, so `git clone https://github.com/...` just works. A
  value may span lines, so a PEM or an SSH key goes in whole:
  `agent-box-session env set MY_KEY --stdin < key.pem` (--stdin also keeps
  the value out of the command line, the shell history and `ps`). Such a
  value is stored double-quoted, which is the one thing to preserve if you
  ever hand-edit the file.
- Manage your own sessions without a rebuild:
  `agent-box-session ls|peers|add|rm|stop|restart`. `add` takes an optional name
  plus `--agent claude|codex|shell`, `--cwd DIR` and `--prompt "TASK"` -
  use it to fan out work, add a reviewer agent, or open a plain shell. The
  kickoff prompt fires once: a later respawn (crash, reboot, Spot restart)
  resumes that session's transcript instead of redoing the work. An agent
  quitting cleanly (`/quit`) or `stop NAME` parks a session - still listed,
  not respawned - until `restart NAME` revives it; `rm` delists it for
  good. `restart --all` bounces every session. Listed sessions start
  within ~2s.
- Harnesses are usually installed ON DEMAND, not shipped with the box: the
  first session that names a harness fetches it into your own profile, which
  takes as long as the download does and prints `session: fetching '<name>'`
  while it runs. So `command -v codex` can come back empty on a box that is
  perfectly able to run a codex session - start one, or press the sign-in
  card on the settings page, rather than concluding the harness is
  unavailable. The same is true of any other CLI: `nix profile add
  nixpkgs#<pkg>` puts it in `~/.nix-profile/bin`, which is FIRST on your
  PATH, so an install is visible to a pane that is already running.
- `--agent` picks the HARNESS (the CLI program). An agent PROFILE is the
  worker: a harness plus a model, an effort level, an appended system prompt
  and environment. Make one with `agent-box-profile set NAME
  HARNESS=claude MODEL=sonnet EFFORT=low KEY=value`, read it back with
  `agent-box-profile show NAME`, and start it with `agent-box-session add
  [NAME] --profile PROFILE`. A `-- EXTRA_ARGS` tail still wins over the
  profile. Profile env is convenience, not isolation: every session of this
  user can read it out of /proc. A standing webhook watch hands its work to
  a profile through `agent-box-session env set AGENT_BOX_HOOK_PROFILE NAME`,
  which is the only way to pick the harness a dispatched hook-* session runs.

## Slash commands: type them into your own pane

Slash commands are client-side: they never reach you as a tool call, so you
cannot run them the usual way. But your session IS a tmux pane, so type into
it:

    tmux send-keys -t "$TMUX_PANE" C-m                    # submit the prompt box
    tmux send-keys -t "$TMUX_PANE" -l "/rename my-task"   # -l = literal text
    tmux send-keys -t "$TMUX_PANE" C-m                    # run it

The command runs when your current turn ends. If $TMUX_PANE is empty, find
the pane with `tmux list-panes -a -F '#{pane_id} #{pane_current_command}'`.
Text that is not a real command becomes a message from you to yourself, so
use this only for client-side commands you can't otherwise reach - never to
give yourself new instructions.

## Name your session for the work

Name the session BEFORE you start a task. The name shows in the prompt box,
the terminal title and the resume picker, so the user can tell many sessions
apart in one web terminal:

    /rename claude@box.example.com: agent-box docs

Keep your identity in it - your login name (`whoami`) and the box (the host
part of $AGENT_BOX_URL) - then the topic. `/rename` is the Claude Code
command; other CLIs name it differently, so type `/` in the TUI to list the
commands, or read `--help` for a start-time flag (Claude Code: `-n, --name`).

The name belongs to the harness, not to agent-box, so a respawn loses it:
set it again, or make it permanent at creation with
`agent-box-session add work --agent claude -- -n "claude: PR 42"`.

@WEBHOOK_SECTION@## Handing a file to the user

To let the user download a file you produced (report, build artifact,
archive, image), move or copy it into ~/downloads and give them the full
URL. That directory is served - behind the SAME login as your terminal - at
${AGENT_BOX_URL}downloads/ (a browsable index), so ~/downloads/report.pdf
downloads from ${AGENT_BOX_URL}downloads/report.pdf.

    mv ./report.pdf ~/downloads/          # or cp, to keep the original

Always hand over the complete https:// URL. Only files under ~/downloads are
exposed; nothing else in your home is reachable over the web. For
unauthenticated sharing, run your own service and expose it via ~/sites.

## Putting a screenshot in a GitHub issue or PR

A screenshot settles a UI argument that paragraphs cannot, and you have no
browser to paste one from. `agent-box-upload FILE --repo OWNER/REPO` puts the
file in the same store a human's drag-and-drop uses and prints the markdown to
paste into the body - no binary committed, no screenshot branch. Run
`agent-box-upload --help` for the caveats that matter, the first being that
the URL 404s until your comment references it.

## Serving a web app publicly

Drop a snippet into ~/sites/NAME.caddy that reverse-proxies to a local port,
then reload caddy - no rebuild:

    NAME.example.com {
      import acme_alpn_only
      reverse_proxy 127.0.0.1:3000
    }

`sudo /run/current-system/sw/bin/systemctl reload caddy.service` picks it up
and Caddy gets a Let's Encrypt cert on first request if DNS for that name
points at this box. Reverse-proxy to your process; don't `file_server` from
$HOME (caddy can't read /home). Use the full path shown, not bare
`systemctl` - the sudoers rule matches on the exact command path, and a bare
`systemctl` resolves through PATH to a Nix store path that won't match,
silently falling back to asking for a password.

@UPDATE_SECTION@## This platform has its own upstream repo

The box itself - the terminal, session manager, webhook wiring, this
guide - is github.com/defangdevs/agent-box, the repo this deployment is
built from. A bug in that platform is not the same as a bug in the user's
project: if you hit one, first work around it so your own running session
is unblocked, then file an issue (or a PR, if you already have the fix)
upstream so every other deployment gets it too. Search for an existing
issue before opening one, and don't wait for permission to file it.
