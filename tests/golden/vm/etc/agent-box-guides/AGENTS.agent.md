# agent-box

You are running inside an agent-box deployment: a coding agent in a
persistent tmux session on a locked-down NixOS host. Your browser
terminal is reachable at $AGENT_BOX_URL (run `echo $AGENT_BOX_URL` to
print it). Share that URL with anyone who needs to view or take over your
session; the sign-in username is your own login name (`whoami`) and the
password was set at deploy time.

The user connects to this box remotely over the web, so whenever you point
them at something the box serves, give the full absolute URL (build it from
$AGENT_BOX_URL) — never a bare local path or a link relative to the
terminal, which a remote user can't act on.

## Your environment

- Only your home directory is writable — the rest of the filesystem is
  read-only (systemd ProtectSystem=strict). Do all work under $HOME;
  writes elsewhere fail with a read-only-filesystem error. The one
  exception is ~/sites, a symlink out to a caddy-readable dir that is
  writable too (see "Serving a web app publicly").
- $HOME is SHARED by every one of your tmux sessions (they all run as the
  same user and start in $HOME). For parallel work in one repo use
  `git worktree` or separate subdirectories, so concurrent sessions don't
  clobber each other's checkout.
- Sessions live in RAM: a reboot loses them, so persist anything worth
  keeping to disk under $HOME. If an agent exits with an error you land in
  a shell for inspection; a clean exit is respawned within ~2s.
- A respawn or reboot starts a fresh context, but each agent keeps its own
  conversation transcripts on disk under $HOME — Claude Code under
  ~/.claude/projects/ (plus ~/.claude/history.jsonl), Codex under
  ~/.codex/sessions/. When you resume after a respawn, or take over a
  session another agent was driving, skim the most recent of these to
  recover what was in flight before writing any code.
- sudo is a tight allowlist (essentially caddy reload + self-update), not
  general root — don't plan around arbitrary sudo.

## Tools, secrets, and sibling sessions

- Install extra tools with nix, e.g. `nix profile add nixpkgs#awscli2`
  (no sudo needed; tools land in ~/.nix-profile/bin, already on PATH).
- Your config lives in the directory ~/.config/agent-box/. Secrets and
  environment variables go in the file `env` there (KEY=value, one per
  line; blank lines and `#` comment lines are ignored, so annotate freely).
  Set them with `agent-box-session env set KEY VALUE` (or `env ls` /
  `env rm KEY`, or the settings page); they load on the next session
  (re)start — e.g. GH_TOKEN is read automatically, so
  `git clone https://github.com/...` just works.
- Manage your own sessions without a rebuild:
  `agent-box-session ls|add|rm|restart`. `add` takes an optional name plus
  `--agent claude|codex|shell`, `--cwd DIR`, and `--prompt "TASK"` to kick
  the session off on a task — handy for fanning out work, spinning up a
  second reviewer agent, or opening a plain shell. The kickoff prompt fires
  once; if that session is later respawned (crash, reboot, Spot restart) the
  supervisor resumes its prior transcript instead of redoing the work.
  `restart --all` bounces every session. Listed sessions start within ~2s.

## Handing a file to the user

To let the user download a file you produced (report, build artifact,
archive, image), move or copy it into ~/downloads and give them the full
URL. That directory is served — behind the SAME login as your terminal —
at ${AGENT_BOX_URL}downloads/ (a browsable index), so a file at
~/downloads/report.pdf downloads from ${AGENT_BOX_URL}downloads/report.pdf.

    mv ./report.pdf ~/downloads/          # or cp, to keep the original

Always hand the user the complete https:// URL. Only files under
~/downloads are exposed this way; nothing else in your home is reachable
over the web. For unauthenticated sharing, run your own web service and
expose it via ~/sites (see below).

## Serving a web app publicly

Drop a snippet into ~/sites/NAME.caddy that reverse-proxies to a local
port, then reload caddy — no rebuild:

    NAME.example.com {
      import acme_alpn_only
      reverse_proxy 127.0.0.1:3000
    }

`sudo systemctl reload caddy.service` picks it up and Caddy gets a Let's
Encrypt cert on first request if DNS for that name points at this box.
Reverse-proxy to your process; don't `file_server` from $HOME (caddy
can't read /home).

## Updating

Update the box's software with:
`sudo systemctl start agent-box-update.service`
(kills the running tmux session — save context first).
