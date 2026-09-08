## Containers: rootless docker, one daemon per user

`docker compose up` works here, and the parts you cannot arrange for
yourself are already in place: an `/etc/subuid` range, `newuidmap`/`newgidmap`
carrying the file capabilities that apply it, and - on a distro that refuses
an unprivileged user namespace outright - the AppArmor exemption that lets
one exist at all.

What is NOT installed is docker. Its closure is about 970 MiB, larger than
the whole runtime profile, so it comes from your own profile like every other
tool on this box:

    nix profile add nixpkgs#docker      # docker, dockerd-rootless, compose v2
    @DOCKER_RESTART_CMD@
    docker compose up

The daemon is a supervised unit, not something you start in a pane: it
survives your session, comes back after a reboot, and there is exactly one
per user. `DOCKER_HOST` is already exported into every session, so no flag
and no `sudo` is needed to USE it - only to restart it, which is what picks
up a newly installed docker (the unit checks for the binary at every start,
and does nothing at all while you have none). `journalctl -u
agent-box-docker@$(whoami)` is where it says why it would not start.

You may `restart` and `stop` your OWN daemon and no one else's. A rootless
daemon is root over its user's containers and home, so that grant stops at
the user boundary like every other one here.

Two things worth knowing before you plan work around it:

- **Images live in your home**, under `~/.local/share/docker`, and they are
  big. On a 2 GiB box, `docker system prune` is part of finishing a task,
  not an afterthought - and check `df -h /` before pulling something large.
- **Containers are not a boundary between SESSIONS.** Every rootless
  container of one user maps into that user's own subuid range, so it
  isolates the user from the box and never one session from its siblings.
  If you want a boundary, the answer is a separate user, not a container.

To deploy a compose file rather than run it, `defang compose up` needs no
local daemon at all.
