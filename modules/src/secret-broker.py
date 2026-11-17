"""agent-box-secret-broker - root custodian for ONE agent user's secrets
(#922).

Runs as root from a socket-activated unit, one instance per configured user.
The socket's directory is root-owned and only that user can connect; on top
of that every connection is checked against the kernel's SO_PEERCRED uid, so
a mis-set mode cannot widen access. The request cannot name a uid, a path,
or a namespace outside this user's own: the user/uid come from this
process's own arguments, and "namespace" is just which of THIS user's
profile buckets a request names.

The protocol (src/lib/secret-protocol.py) has no list/set/remove operation
that returns a value. exec/status/cancel (src/lib/secret-jobs.py) are the
only ops that ever let a value leave this process, and they hand it to a
worker that is already running as the unprivileged agent user by the time it
sees it - never back to the caller.
"""
import argparse
import os
import pwd
import subprocess
import sys
import threading
import time


def peer_uid(conn):
    cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", cred)[1]


def drain_request(conn):
    """Read the peer's frame before refusing it. Closing with the request
    still unread resets the connection, so the peer sees EPIPE or ECONNRESET
    instead of the refusal."""
    try:
        recv_frame(conn)
    except (SecretError, OSError):
        pass


class SystemdLauncher(Launcher):
    """Production launcher: a systemd transient service with User=<agent>,
    one per job. `systemd-run --pipe` relays the unit's stdio to this
    process's own pipes, so from JobManager's point of view this behaves
    exactly like DirectLauncher's subprocess.Popen - poll()/kill() apply to
    the systemd-run process, which blocks for the unit's whole lifetime.

    Unproven on this box: there is no root and no KVM here, so this class
    has never been exercised end to end. The OS boundary it is supposed to
    give (uid, cgroup, NoNewPrivileges) is exactly what tests/secrets.nix is
    for; see the issue and the PR description before trusting it in
    production. DirectLauncher is what every unit test in this repository
    actually runs.
    """

    def __init__(self, user, worker_path, python_bin=None):
        self.user = user
        self.worker_path = worker_path
        # None: exec the worker by its own absolute path. It is a generated
        # script with a store-path shebang, so nothing here depends on a
        # bare `python3` being on the PATH of the broker's unit or of the
        # transient job unit - neither of which has one on NixOS.
        self.python_bin = python_bin

    @staticmethod
    def _unit_name(job):
        return f"agent-box-secret-job-{job.id}"

    def start(self, job, argv_or_command, shell, secret_values, nonsecret_env, cwd,
              browser=None):
        frame_bytes = encode_worker_frame(argv_or_command, shell, secret_values,
                                          nonsecret_env, cwd, browser)
        cmd = [
            "systemd-run", "--pipe", "--quiet", "--collect",
            f"--unit={self._unit_name(job)}",
            f"--property=User={self.user}",
            "--property=KillMode=control-group",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=TimeoutStopSec=5",
            "--",
            *([self.python_bin] if self.python_bin else []), self.worker_path,
        ]
        job.handle = spawn_worker(cmd, frame_bytes)
        return job.handle

    def poll(self, job):
        return job.handle.poll()

    def kill(self, job):
        if job.handle is None:
            return
        subprocess.run(["systemctl", "kill", "--signal=SIGKILL", self._unit_name(job)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            job.handle.kill()
        except OSError:
            pass


class Broker:
    # A profile name is cheap for a CALLER to mint (any string matching
    # PROFILE_NAME_RE), and each new one costs this process a directory, a
    # meta.json, a lock fd and a cached Store with its own open dir_fd. None
    # of that is reclaimed on its own, so without a bound the agent user can
    # exhaust this root process's file descriptors just by asking `list` for
    # enough distinct names. Evicting the least-recently-used Store when the
    # cache is full bounds the descriptors; a profile that is actually in use
    # stays resident because it keeps getting touched. Eviction never deletes
    # anything on disk: the persistent namespaces are bounded separately, by
    # SECRET_MAX_NAMESPACES_PER_USER in Store.
    MAX_OPEN_STORES = 64

    def __init__(self, user, uid, store_root, root_key, job_manager):
        self.user = user
        self.uid = uid
        self.store_root = store_root
        self.root_key = root_key
        self.job_manager = job_manager
        self._stores = {}
        self._stores_lock = threading.Lock()

    def _store(self, profile, create):
        """The cached Store for this profile. With create=False (every op
        but `set`) a namespace that does not exist yet is NOT made: the
        answer is None, so a `list` or `exec` naming an invented profile
        costs nothing on disk and no slot of SECRET_MAX_NAMESPACES_PER_USER.
        """
        # Already validated by validate_request (every request names a
        # concrete profile - see resolve_profile's docstring): "profile" is
        # the namespace, with no further resolution needed here.
        namespace = profile
        with self._stores_lock:
            store = self._stores.pop(namespace, None)
            if store is None:
                if not create and not Store.exists(self.store_root, namespace):
                    return None
                store = Store(self.store_root, namespace, self.user, self.uid, self.root_key)
            self._stores[namespace] = store  # re-insert: most-recently-used
            while len(self._stores) > self.MAX_OPEN_STORES:
                evicted_name = next(iter(self._stores))
                if evicted_name == namespace:
                    break
                self._stores.pop(evicted_name).close()
            return store

    def handle(self, req):
        op = validate_request(req)
        profile = req.get("profile")
        # status/cancel only need the job manager, which keys jobs by the
        # profile name; they never touch the store.
        if op == "status":
            return {"ok": True, **self.job_manager.status(profile, req["jobId"])}
        if op == "cancel":
            return {"ok": True, **self.job_manager.cancel(profile, req["jobId"])}
        store = self._store(profile, create=(op in ("set", "generate", "save-cookies")))
        if op in ("list", "set", "remove", "generate"):
            if store is None:
                if op == "list":
                    return {"ok": True, "secrets": []}
                raise SecretError("no such secret")
            return handle_request(store, req)
        if op == "exec":
            # A missing namespace resolves no name, so _dispatch reports
            # "no such secret: NAME" for each; with no secretNames the job
            # simply runs.
            resolve = self._exec_resolver(store)
            job_id = self.job_manager.start(profile, req, resolve)
            return {"ok": True, "jobId": job_id}
        if op in ("fill", "save-cookies", "load-cookies"):
            builtin = self._browser_job(store, profile, req, op)
            job_id = self.job_manager.start(profile, req, self._fill_resolver(store), builtin)
            return {"ok": True, "jobId": job_id}
        raise SecretError("unsupported operation")

    @staticmethod
    def _exec_resolver(store):
        """What exec may inject for a name: the value, a TOTP secret's
        current code (never its seed), and nothing at all for a fill-only
        secret - that one goes only to the fill op, onto its own origins."""
        def resolve(name):
            if store is None:
                return None
            meta = store.meta(name)
            if meta is None:
                return None
            if meta["fillOnly"]:
                raise SecretError(f"{name} is fill-only: use fill, not exec")
            value = store.get(name)
            if value is not None and meta["kind"] == "totp":
                return totp_code(value)
            return value
        return resolve

    @staticmethod
    def _fill_resolver(store):
        def resolve(name):
            value = store.get(name)
            if value is not None and store.meta(name)["kind"] == "totp":
                return totp_code(value)
            return value
        return resolve

    def _browser_job(self, store, profile, req, op):
        """The fixed worker-side CDP job for one browser op. The origins
        that bound it come from the RECORD (fill, load-cookies) or from the
        request that is about to replace the record (save-cookies) - never
        from the page, which is what the worker checks against them."""
        name = req["name"]
        browser = {"mode": op, "port": req["cdpPort"], "targetId": req.get("targetId")}
        if op == "save-cookies":
            browser["origins"] = list(req["origins"])
            meta = record_meta_from_request(req, kind="cookies")
            return {"secretNames": [], "browser": browser,
                    "capture": self._cookie_sink(profile, name, meta)}
        meta = store.meta(name) if store is not None else None
        if meta is None:
            raise SecretError(f"no such secret: {name}")
        if not meta["origins"]:
            raise SecretError(f"{name} has no origins: set it again with --origin")
        if op == "fill" and meta["kind"] == "cookies":
            raise SecretError(f"{name} holds cookies: use load-cookies")
        if op == "load-cookies" and meta["kind"] != "cookies":
            raise SecretError(f"{name} does not hold cookies")
        browser["origins"] = meta["origins"]
        browser["secret"] = name
        if op == "fill":
            browser["selector"] = req["selector"]
        return {"secretNames": [name], "browser": browser}

    def _cookie_sink(self, profile, name, meta):
        """Stores what a save-cookies worker printed. Runs on the job's
        drain thread, so it opens a Store of its own rather than borrow the
        cached one, which the serving thread may evict and close meanwhile."""
        def sink(captured):
            value = captured.decode("utf-8")
            if not valid_secret_value(value):
                raise SecretError("captured cookies are empty or too large to store")
            count = len(parse_cookie_state(value))
            store = Store(self.store_root, profile, self.user, self.uid, self.root_key)
            try:
                version = store.set(name, value, meta)
            finally:
                store.close()
            return f"saved {count} cookies as {name} (v{version})\n"
        return sink


def _safe_send(conn, reply):
    """send_frame itself can refuse (SecretError("response too large")): a
    job's own (already-capped) output plus the JSON metadata can pass
    SECRET_MAX_FRAME, and in the pathological case (every byte a control
    character escapes to \\u00NN, a 6x blow-up) by a lot. A reply that
    carries output is shrunk - halving stdout/stderr and marking them
    truncated - until it fits, so the caller still gets the job's state, its
    exit code and what output fits. Only a reply that cannot be made to fit
    at all becomes a refusal, and that one says ok=false."""
    try:
        send_frame(conn, reply)
        return
    except SecretError:
        pass
    shrunk = dict(reply)
    for _ in range(16):
        for key in ("stdout", "stderr"):
            text = shrunk.get(key)
            if isinstance(text, str) and text:
                shrunk[key] = text[:len(text) // 2]
                shrunk[key + "Truncated"] = True
        try:
            send_frame(conn, shrunk)
            return
        except SecretError:
            continue
    send_frame(conn, {"ok": False,
                      "error": "response too large to return in full",
                      "jobId": reply.get("jobId"), "state": reply.get("state")})


def serve_one(listener, broker, expect_uid):
    try:
        conn, _ = listener.accept()
    except OSError:
        return
    with conn:
        conn.settimeout(10)
        try:
            if peer_uid(conn) != expect_uid:
                drain_request(conn)
                _safe_send(conn, {"ok": False, "error": "forbidden"})
                return
            try:
                reply = broker.handle(recv_frame(conn))
            except SecretError as exc:
                reply = {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - see comment below
                # Anything else is a bug in this request's handling, not a
                # reason to take every OTHER connection down with it: this
                # is a long-lived root process serving every session of one
                # user, and "the broker died" means every one of that
                # user's in-flight jobs and secrets becomes unreachable
                # until something restarts it. Never let an unanticipated
                # exception propagate out of one connection.
                reply = {"ok": False, "error": f"internal error: {type(exc).__name__}"}
            _safe_send(conn, reply)
        except (OSError, socket.timeout):
            # A peer that vanished mid-exchange must never stop the broker.
            pass


def _sweeper(job_manager):
    while True:
        time.sleep(300)
        try:
            job_manager.sweep(SECRET_JOB_RETENTION_SECONDS)
        except Exception as exc:  # noqa: BLE001 - a daemon thread must not die silently
            sys.stderr.write(f"agent-box-secret-broker: sweep failed: {exc}\n")


def main():
    # Config is environment, not argv, in production - the unit sets
    # AGENT_BOX_SECRET_USER/_STORE_ROOT/_WORKER_BIN from %i and the
    # contract (see units/agent-box-secret-broker@.service and
    # contract/agent-box-secret-broker.json). Explicit flags remain for
    # direct/test invocation, same pattern as secret-cli.py's --socket.
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=os.environ.get("AGENT_BOX_SECRET_USER"))
    parser.add_argument("--uid", type=int, default=None,
                        help="defaults to --user's real uid (pwd.getpwnam); pass this "
                             "explicitly only to test the SO_PEERCRED mismatch path")
    parser.add_argument("--store-root", default=os.environ.get("AGENT_BOX_SECRET_STORE_ROOT"),
                        help="this user's own directory, e.g. /var/lib/agent-box-secrets/<user>")
    parser.add_argument("--root-key-path",
                        help="box-wide key file; defaults beside --store-root's parent")
    parser.add_argument("--worker",
                        default=os.environ.get("AGENT_BOX_SECRET_WORKER_BIN",
                                               "/run/current-system/sw/bin/agent-box-secret-worker"),
                        help="path to the fixed worker executable")
    parser.add_argument("--socket", help="bind this path instead of using systemd's fd 3")
    parser.add_argument("--dev-direct-launcher", action="store_true",
                        help="DEV/TEST ONLY: run jobs with the broker's own uid instead of "
                             "systemd User=<agent>. Refused when this process is root (it would "
                             "execute the workload AS ROOT), and refused unless "
                             "AGENT_BOX_SECRET_BROKER_ALLOW_DEV_LAUNCHER=1 is also set.")
    args = parser.parse_args()

    if not args.user or not args.store_root:
        sys.stderr.write(
            "agent-box-secret-broker: --user and --store-root are required "
            "(directly, or via AGENT_BOX_SECRET_USER/AGENT_BOX_SECRET_STORE_ROOT)\n")
        raise SystemExit(2)
    if args.uid is None:
        try:
            args.uid = pwd.getpwnam(args.user).pw_uid
        except KeyError:
            sys.stderr.write(f"agent-box-secret-broker: no such user: {args.user}\n")
            raise SystemExit(2)

    if args.dev_direct_launcher:
        # Two independent gates, not one: a single stray argv flag (a typo'd
        # unit override, a copy-pasted systemd drop-in) must not be enough
        # to make a production broker run every job as ITS OWN uid instead
        # of dropping to the agent user. The env var is the one a real
        # deployment's unit file never sets.
        if os.getuid() == 0:
            sys.stderr.write(
                "agent-box-secret-broker: --dev-direct-launcher refused while running as root "
                "(it would execute jobs as root, defeating the whole feature)\n")
            raise SystemExit(2)
        if os.environ.get("AGENT_BOX_SECRET_BROKER_ALLOW_DEV_LAUNCHER") != "1":
            sys.stderr.write(
                "agent-box-secret-broker: --dev-direct-launcher also needs "
                "AGENT_BOX_SECRET_BROKER_ALLOW_DEV_LAUNCHER=1 in the environment\n")
            raise SystemExit(2)

    if args.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(args.socket)
        # fchmod on the fd, not chmod on the path: bind() just created this
        # exact file, so there is nothing left for a path-based chmod to buy
        # and no window for it to be tricked by a symlink planted at that
        # path between the two calls.
        os.fchmod(listener.fileno(), 0o660)
        listener.listen(8)
    elif os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        listener = socket.socket(fileno=3)
    else:
        sys.stderr.write("agent-box-secret-broker: no listening socket\n")
        raise SystemExit(2)

    root_key_path = args.root_key_path or os.path.join(
        os.path.dirname(os.path.normpath(args.store_root)), ".rootkey")
    root_key = load_root_key(root_key_path)

    if args.dev_direct_launcher:
        launcher = DirectLauncher(args.worker)
    else:
        launcher = SystemdLauncher(args.user, args.worker)
    job_manager = JobManager(launcher, SECRET_MAX_OUTPUT_BYTES, SECRET_MAX_JOBS_PER_USER)
    threading.Thread(target=_sweeper, args=(job_manager,), daemon=True).start()

    broker = Broker(args.user, args.uid, args.store_root, root_key, job_manager)
    while True:
        serve_one(listener, broker, args.uid)


if __name__ == "__main__":
    main()
