# agent-box

You run inside an agent-box deployment: a coding agent in a persistent tmux
session on a locked-down host. (What KIND of host is "Your host" below -
agent-box deploys as a NixOS system and as a Nix profile on an ordinary
distro, and the two differ in ways worth knowing before you change
anything outside $HOME.) Your workspace is at $AGENT_BOX_URL
(`echo $AGENT_BOX_URL` prints it): one tab per session, and each session also
has a terminal of its own at ${AGENT_BOX_URL}<session>/. The grid button at
the end of the tab bar (or ${AGENT_BOX_URL}?view=grid) shows every session at
once, which is the URL to hand somebody who is watching several agents work. Share those URLs
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
  That post-mortem shell keeps the tmux session ALIVE with no agent in it,
  so nothing restarts it and nothing is reading events for it. Every
  surface says which one it is: `agent-box-session ls` prints
  `died(<status>)`, `peers` marks it DIED (a session that died is not an
  owner to yield to), and the settings page and workspace tab show it in
  red. Restart it from that page, or with
  `agent-box-session restart NAME`, once you know why it went.
- A respawn or reboot starts a fresh context, but transcripts stay on disk -
  Claude Code under ~/.claude/projects/ (plus ~/.claude/history.jsonl),
  Codex under ~/.codex/sessions/. After a respawn, or when you take over
  another agent's session, skim the most recent one before writing code.
- A Claude or Codex session that comes back from a respawn is RESUMED, not
  restarted: the transcript is restored, but no turn starts by itself. So if
  you are holding work when the box goes down - an open PR, a claimed issue -
  you wake up silent and nobody is coming to type. The box sends a short "you
  were interrupted" prompt to start one turn - but only when it can SEE that
  you had something open: a claimed hook assignment, a webhook subscription,
  or (for Claude) a turn the kill cut in half. Work it cannot see - an open PR
  you never subscribed to - wakes nobody, so subscribe to what you are waiting
  on rather than counting on the nudge. A remote-controlled Codex task is
  targetable only after it subscribes: the CLI remembers that exact app task
  so the restarted daemon can queue the notice to it.
  When one does arrive, do not trust the transcript alone. No sender replays
  what fired while you were down, so read the CURRENT state of whatever you
  were waiting on and check `agent-box-webhook status`. A full service restart
  also ends Codex's detached delivery peer even though its filter survives;
  re-run the subscription command if status shows no live peer.
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
- Agent session starts share one limit across the CLI, settings page and
  webhooks. Shell panes are operator terminals and do not use a slot. The
  limit defaults to about one session per GiB of physical RAM and can be
  overridden by `sessionLimit` in the box configuration. Pending starts reserve
  slots too. Stop a session to free capacity; restarting a stopped session
  needs a free slot. `restart --all` refuses without changing anything if it
  would exceed the limit. When a new runtime is refused at the limit,
  Agentbox may hibernate the oldest conservatively idle Claude or Codex TUI
  session: its process exits, its stable name and native conversation ID stay
  recorded, and a later routed message transparently resumes that exact
  conversation. It never hibernates merely because a session is idle. An open
  turn, unresolved assignment, webhook subscription, pending delivery,
  unknown transcript state, shell, hook session, or Claude/Codex Remote
  Control server remains resident. `sessionHibernationGraceSeconds` sets the
  minimum age of the completed turn (30 seconds by default). This is overload
  control, not a memory guarantee.
- Manage your own sessions without a rebuild:
  `agent-box-session ls|peers|add|rm|stop|restart`. `add` takes an optional name
  plus `--harness claude|codex|shell`, `--cwd DIR` and `--prompt "TASK"` -
  use it to fan out work, add a reviewer agent, or open a plain shell. The
  kickoff prompt fires once: a later respawn (crash, reboot, Spot restart)
  resumes that session's transcript instead of redoing the work. An agent
  quitting cleanly (`/quit`) or `stop NAME` parks a session - still listed,
  not respawned - until `restart NAME` revives it; `rm` delists it for
  good. `restart --all` bounces every session. Listed sessions start
  within ~2s.
- Remote Control is a per-conversation capability; daemon mode is the
  persistent process that can serve several conversations. Claude's default
  `--remote-control true` keeps one ordinary TUI and mirrors it to the Claude
  apps, matching `/rc` inside a running session. Use
  `agent-box-session add NAME --harness claude --daemon` for
  the persistent `claude remote-control` host instead. That one agent-box row
  consumes one slot and serves several app-created conversations, which do
  not become separate rows or consume agent-box slots. It has no single
  kickoff prompt, transcript download, webhook or WhatsApp target, or tmux
  conversation pane. Limit those native child conversations with Claude's
  own server flags after `--`, such as `-- --capacity 4`.
  `agent-box-session add NAME --harness codex --daemon` starts the equivalent
  Codex app-server pairing daemon. The old `--remote-control server` spelling
  is accepted for compatibility.
- Harnesses are usually installed ON DEMAND, not shipped with the box: the
  first session that names a harness fetches it into your own profile, which
  takes as long as the download does and prints `session: fetching '<name>'`
  while it runs. So `command -v codex` can come back empty on a box that is
  perfectly able to run a codex session - start one, or press the sign-in
  card on the settings page, rather than concluding the harness is
  unavailable. The same is true of any other CLI: `nix profile add
  nixpkgs#<pkg>` puts it in `~/.nix-profile/bin`, which is FIRST on your
  PATH, so an install is visible to a pane that is already running.
- `--harness` picks the CLI PROGRAM (claude, codex, shell). An agent PROFILE
  is the worker: a harness plus a model, an effort level, an appended system
  prompt and environment. Keep the two words apart, because the harnesses
  themselves do not: `claude --agent` and `opencode --agent` both name a
  WORKER, which here is `--profile`. Our own `--agent` was the old spelling
  of `--harness`; it still works and prints a deprecation line. Make a
  profile with `agent-box-profile set NAME
  HARNESS=claude MODEL=sonnet EFFORT=low KEY=value`, read it back with
  `agent-box-profile show NAME`, and start it with `agent-box-session add
  [NAME] --profile PROFILE`. A `-- EXTRA_ARGS` tail still wins over the
  profile. Signing in to Claude or Codex creates that harness's starter
  profile if needed and, when no valid default exists, makes it the default.
  A later sign-in never replaces an existing default. Profile env is
  convenience, not isolation: every session of this
  user can read it out of /proc. A standing webhook watch hands its work to
  a profile through `agent-box-session env set AGENT_BOX_HOOK_PROFILE NAME`,
  which is how the harness a dispatched hook-* session runs gets picked at
  all. That setting is box-wide; one watch picks its OWN worker with
  `agent-box-webhook subscribe TOPIC --deliver-to subagent --profile NAME`,
  which beats it. So cheap triage can take new issues while a red build
  starts something that can fix it.
- One profile can be the DEFAULT: `agent-box-profile default NAME` (or the
  star on its row in settings). It is preselected in every "new session"
  picker and used by `agent-box-session add` given neither `--profile` nor
  `--harness`. A standing watch never uses it: a watch's worker is always
  the one it names (or `AGENT_BOX_HOOK_PROFILE`), so changing the default
  cannot change what an event starts. `agent-box-profile default --clear`
  unsets it, and deleting the default profile clears it too - the next
  session then asks which profile to start.

## Testing Buzz with a remote Agentbox profile

`agent-box-acp` is an opt-in ACP stdio endpoint for Buzz's existing
`BUZZ_ACP_LAUNCH_PREFIX`. Buzz keeps channel/thread routing and ACP session
reuse; the endpoint pins one Agentbox profile and one remote working directory.
It does not attach to a tmux session or select an old native conversation.

Check a binding on the box before putting it in an SSH key's forced command:

    agent-box-acp check --local --profile devops \
      --adapter /absolute/path/to/codex-acp \
      --native /absolute/path/to/codex \
      --cwd /home/agent/project

Then use the same binding arguments after `agent-box-acp serve` in a dedicated
`authorized_keys` entry with OpenSSH's `restrict,command="..."` options, but
omit `--local` so the forced command checks Buzz's requested worker. Use
the absolute path printed by `command -v agent-box-acp` in that entry (the
stable path differs between NixOS and native hosts). Buzz's
launch prefix is a JSON argv array ending in `user@host`, for example
`["/usr/bin/ssh","-T","-o","BatchMode=yes","-o","IdentitiesOnly=yes",
"-o","StrictHostKeyChecking=yes","-i","/path/to/key","user@host"]`.
Buzz appends the real worker name; the endpoint accepts exactly `codex-acp` for
a Codex profile or `claude-agent-acp` for a Claude profile by default. Use
`--expected-command` only when the registered adapter has another fixed name,
and leave the Buzz custom harness's adapter argument list empty.

The endpoint rewrites `session/new.cwd` and Buzz prompt path anchors to the
fixed remote directory, applies the profile's ordinary environment at launch,
and composes Claude model/effort/appended instructions through ACP metadata.
Codex gets the profile's native arguments literally and keeps AGENTS.md as its
instruction path. Non-empty MCP server definitions are refused by default,
because a local executable path does not become a remote one over SSH. Add
`--allow-mcp` only after every command and path in the request is already valid
on the box. This first endpoint also does not copy Buzz-local Git credentials,
helper binaries or skill files; install the equivalents on the box or leave
features that require them disabled.

One worker per profile is allowed; one worker can hold several Buzz sessions.
Remote ACP workers do not consume tmux session slots, so keep the number of
exposed profiles within the host's resource budget. EOF, SSH loss and signals
terminate the owned adapter/native process group; disconnect is never reported
as successful completion. Native auth must already exist on the box. The forced
key grants trusted native-agent execution as this Linux user, including shell
tools requested through ACP. A profile is scoping, not isolation; use a separate
Linux user for a separate trust domain.

## Personal WhatsApp connection

The settings page's Connections section can link this user's WhatsApp as a
device. Enter the account's phone number with country code, start pairing,
and give the displayed code to WhatsApp's Linked devices screen on the
primary phone. Text must start with `@agent ` (`@` plus this box's Linux user).
Media must either be captioned that way or sent during an explicitly opened
ten-minute upload window. The bridge accepts them only from that
account's Message Yourself chat. The device link belongs to the Linux user and
survives agent session restarts. Automatic message receipts are off by default;
set `LOCAL_WHATSAPP_DEBUG=1` in the env store and restart the bridge to enable
diagnostic receipts. Agent replies and explicit command responses stay enabled.
Node and the bridge are installed only when
pairing is requested; they are not part of the base image. The supervisor
runs the bridge without spending a session slot. Message text and linked-device
keys are stored in private files under `~/.local/state/local-whatsapp` on this
box; WhatsApp's end-to-end encryption covers the chat transport.

Use **Unlink** on the WhatsApp connection card, or run
`agent-box-whatsapp unlink`, to stop the bridge and erase its linked-device
keys, queued messages and routing state from this box. The optional runtime
stays installed so pairing again does not need another download. WhatsApp may
continue to list the now-inactive device until it is also removed from the
primary phone's Linked devices screen.

WhatsApp has one destination session at a time. From the phone, send
`@agent /sessions` to list Claude and Codex sessions, then `@agent /target NAME`
to choose one. `@agent /target auto` clears that choice, so the next message
starts a new session with the profile selected in Connections; with no
selection, it uses the box's default profile. Send `@agent /profile NAME` to
change the profile and make the next message start fresh, or
`@agent /profile default` to use the default profile. The CLI equivalents are
`agent-box-session whatsapp candidates`, `select NAME`, `activate NAME`,
`clear`, and `spawn PROFILE|default`. A stopped session is a cold conversation:
the next message asks the shared capacity policy to reactivate that exact
session and resume its native harness thread. If no slot is available or that
selected session no longer exists, the message remains queued on the same
target instead of starting a replacement.
Selecting a different target does not require pairing again. Restart a running
Claude session after selecting it so it loads the WhatsApp channel.

A normal Codex TUI is registered automatically after its rollout starts, so
WhatsApp resolves the agent-box session name to the real Codex thread ID. A
Codex Remote Control task has its own thread ID, which the box's session name
alone cannot identify. A Claude Remote Control daemon likewise owns several
app-created conversations and cannot be selected as one WhatsApp target. A
Codex task with an active webhook subscription is also
registered automatically; otherwise, from inside the active Codex task run `node
~/.local/share/local-whatsapp/bridge.mjs register codex` once to bind it to
that session name. Repeat after a different task takes over the same session.
For a native image reply,
run `node ~/.local/share/local-whatsapp/bridge.mjs reply-image MESSAGE_ID /absolute/path/picture.png "caption"`. Claude uses
`whatsapp_reply_image` with the message ID, absolute image path, and optional
caption. PNG, JPEG, and WebP files are supported, up to 10 MiB, with at most
20 queued images. The bridge keeps private copies for retries and removes them
after sending. No download link or re-pairing is needed.

For an inbound image, video, or document, add `@agent` at the start of its
caption (a caption containing only `@agent` works). To send captionless media,
first send `@agent /attach`; that opens a ten-minute upload window for this
account's Message Yourself chat. An agent can open the same window with `node
~/.local/share/local-whatsapp/bridge.mjs receive-files`. View-once media is not
imported. Each attachment is limited to 20 MiB and the private inbox to 200
MiB. The bridge gives the agent the local path and metadata but never executes
or extracts the untrusted filename or contents. Files remain private on this
box and are cleaned up with their retained messages. The bridge has no shell
command target.

## Slash commands: type them into your own pane

Slash commands are client-side: they never reach you as a tool call, so you
cannot run them the usual way. But your session IS a tmux pane, so type into
it:

    tmux send-keys -t "$TMUX_PANE" C-m                    # submit the prompt box
    tmux send-keys -t "$TMUX_PANE" -l "/rename my-task"   # -l = literal text
    tmux send-keys -t "$TMUX_PANE" C-m                    # run it

This applies only when the interactive agent TUI is running in that pane. A
conversation opened from an app through a Claude or Codex Remote Control
server inherits `$TMUX_PANE` from the shared server, not from a conversation
pane. Never send keys there from a remote conversation, including to rename
yourself: there is no agent prompt there, and server panes have controls of
their own (issue #691). Rename a Codex conversation through the app instead:
use its
`set_thread_title` tool when available (with no thread id, it targets the
calling thread). An app-server client can make the equivalent
`thread/name/set` request with that conversation's `threadId` and the new
title as `name`.

The three commands explicitly target $TMUX_PANE. If it is empty, recover your
pane id by matching the command's controlling TTY before sending any keys:

    pane_tty="$(tty)"
    TMUX_PANE="$(
      tmux list-panes -a -F '#{pane_id} #{pane_tty}' |
        awk -v pane_tty="$pane_tty" '$2 == pane_tty { print $1; exit }'
    )"
    test -n "$TMUX_PANE"

If `tty` or the final check fails, do not send keys. Bare `tmux display-message`
is not a safe fallback: without $TMUX_PANE, $TMUX identifies the server and
session but tmux reports that session's active pane, which need not be the
caller's. Do not guess from `pane_current_command` either: the launcher wrapper
remains the foreground process in every agent pane, so they all report the
same command and a wrong guess sends a stray command to a sibling session
(issue #691).
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
For a Codex Remote Control conversation, satisfy this step with the Codex
app's `set_thread_title` tool instead. If it is unavailable, leave the title
for the user to set in the app. Do the same for an app-created Claude server
conversation; never send `/rename` to either shared server's tmux pane.

The name belongs to the harness, not to agent-box, so a respawn loses it:
set it again, or make it permanent at creation with
`agent-box-session add work --harness claude -- -n "claude: PR 42"`.

@CONTAINERS_SECTION@@WEBHOOK_SECTION@## Receiving a large file from the user

When the user needs to give this box a file that is too large for chat, mint
one temporary resumable upload link:

    agent-box-receive --ttl 4h --max-size 2G

Hand them the complete URL the command prints. The part after `#` is the
upload credential: treat the whole link as a secret and revoke it if it is
sent to the wrong person. It authorizes one file up to the stated size and
never signs the holder into the terminal or settings page. Interrupted browser
uploads resume automatically, and the completed file appears in `~/uploads`.

`agent-box-receive list` shows active slots. `agent-box-receive revoke SLOT`
invalidates one immediately and removes its unfinished bytes. Expired slots
are removed opportunistically. Do not open the upload URL yourself before
handing it over if your chat client or browser sync might expose its fragment.

## Handing a file to the user

To let the user download a file you produced (report, build artifact,
archive, image), move or copy it into ~/downloads and give them the full
URL. That directory is served - behind the SAME login as your terminal - at
${AGENT_BOX_URL}downloads/ (a browsable index), so ~/downloads/report.pdf
downloads from ${AGENT_BOX_URL}downloads/report.pdf.

    mv ./report.pdf ~/downloads/          # or cp, to keep the original

Always hand over the complete https:// URL. Only files under ~/downloads are
exposed; nothing else in your home is reachable over the web - a symlink out
of the directory included, since a link is followed only where it stays
inside the drop, so `ln -s ~/build/big.tar ~/downloads/` reads as a missing
file. For unauthenticated sharing, run your own service and expose it via
~/sites.

Every file there is handed to the browser as a DOWNLOAD, never rendered:
this directory shares an origin with the terminal and the settings page, so
an .html or .svg opened inline would be script running with the user's own
login. That is a deliberate trade - a report you drop here is saved, not
read in the tab - so if you want the user to LOOK at something in their
browser rather than save it, serve it yourself through ~/sites, which is a
separate hostname with none of that authority. An `index.html` in
~/downloads is not served either; the listing is always the listing.

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

`systemctl reload caddy.service` picks it up
and Caddy gets a Let's Encrypt cert on first request if DNS for that name
points at this box. Reverse-proxy to your process; don't `file_server` from
$HOME (caddy can't read /home). The command needs no sudo: agent-box grants
configured agent users exactly the reload verb on `caddy.service` through
polkit. Use the `systemctl` on your PATH; if there is none (the NixOS agent
PATH is curated), use `/run/current-system/sw/bin/systemctl`, which a distro
box does not have. Stop, restart and every other unit stay unauthorized. This path also
works inside Codex's unprivileged command sandbox, where setuid sudo is
unavailable (agent-box#726).

@UPDATE_SECTION@## This platform has its own upstream repo

The box itself - the terminal, session manager, webhook wiring, this
guide - is github.com/defangdevs/agent-box, the repo this deployment is
built from. A bug in that platform is not the same as a bug in the user's
project: if you hit one, first work around it so your own running session
is unblocked, then file an issue (or a PR, if you already have the fix)
upstream so every other deployment gets it too. Search for an existing
issue before opening one, and don't wait for permission to file it.
