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

    def __init__(self, user, worker_path, python_bin="python3"):
        self.user = user
        self.worker_path = worker_path
        self.python_bin = python_bin

    def start(self, job, argv_or_command, shell, secret_values, nonsecret_env, cwd):
        frame = json.dumps({
            "argv": argv_or_command if shell is None else None,
            "command": argv_or_command if shell is not None else None,
            "shell": shell,
            "secrets": secret_values,
            "env": nonsecret_env,
            "cwd": cwd,
        }).encode("utf-8")
        unit = f"agent-box-secret-job-{job.id}"
        job.unit_name = unit
        cmd = [
            "systemd-run", "--pipe", "--quiet", "--collect",
            f"--unit={unit}",
            f"--property=User={self.user}",
            "--property=KillMode=control-group",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=TimeoutStopSec=5",
            "--",
            self.python_bin, self.worker_path,
        ]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.stdin.write(len(frame).to_bytes(4, "big") + frame)
        proc.stdin.close()
        job.handle = proc
        return proc

    def poll(self, job):
        return job.handle.poll()

    def kill(self, job):
        unit = getattr(job, "unit_name", None)
        if unit:
            subprocess.run(["systemctl", "kill", "--signal=SIGKILL", unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            job.handle.kill()
        except OSError:
            pass


class Broker:
    def __init__(self, user, uid, store_root, root_key, job_manager):
        self.user = user
        self.uid = uid
        self.store_root = store_root
        self.root_key = root_key
        self.job_manager = job_manager
        self._stores = {}
        self._stores_lock = threading.Lock()

    def _store(self, profile):
        namespace = normalize_profile(profile)
        with self._stores_lock:
            store = self._stores.get(namespace)
            if store is None:
                store = Store(self.store_root, namespace, self.user, self.uid, self.root_key)
                self._stores[namespace] = store
            return store

    def handle(self, req):
        op = validate_request(req)
        store = self._store(req.get("profile"))
        if op in ("list", "set", "remove"):
            return handle_request(store, req)
        if op == "exec":
            job_id = self.job_manager.start(store.namespace, req, store.get)
            return {"ok": True, "jobId": job_id}
        if op == "status":
            return {"ok": True, **self.job_manager.status(store.namespace, req["jobId"])}
        if op == "cancel":
            return {"ok": True, **self.job_manager.cancel(store.namespace, req["jobId"])}
        raise SecretError("unsupported operation")


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
                send_frame(conn, {"ok": False, "error": "forbidden"})
                return
            try:
                reply = broker.handle(recv_frame(conn))
            except SecretError as exc:
                reply = {"ok": False, "error": str(exc)}
            send_frame(conn, reply)
        except (OSError, socket.timeout):
            # A peer that vanished mid-exchange must never stop the broker.
            pass


def _sweeper(job_manager):
    while True:
        time.sleep(300)
        job_manager.sweep(SECRET_JOB_RETENTION_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--store-root", required=True,
                        help="this user's own directory, e.g. /var/lib/agent-box-secrets/<user>")
    parser.add_argument("--root-key-path",
                        help="box-wide key file; defaults beside --store-root's parent")
    parser.add_argument("--worker",
                        default="/run/current-system/sw/bin/agent-box-secret-worker",
                        help="path to the fixed worker executable")
    parser.add_argument("--socket", help="bind this path instead of using systemd's fd 3")
    parser.add_argument("--dev-direct-launcher", action="store_true",
                        help="DEV/TEST ONLY: run jobs with the broker's own uid instead of "
                             "systemd User=<agent>. Refused when this process is root, because "
                             "that would execute the workload AS ROOT.")
    args = parser.parse_args()

    if args.dev_direct_launcher and os.getuid() == 0:
        sys.stderr.write(
            "agent-box-secret-broker: --dev-direct-launcher refused while running as root "
            "(it would execute jobs as root, defeating the whole feature)\n")
        return 2

    if args.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(args.socket)
        os.chmod(args.socket, 0o660)
        listener.listen(8)
    elif os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        listener = socket.socket(fileno=3)
    else:
        sys.stderr.write("agent-box-secret-broker: no listening socket\n")
        return 2

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
    sys.exit(main())
