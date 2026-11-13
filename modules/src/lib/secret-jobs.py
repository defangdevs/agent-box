# Secret custody: bounded command jobs (issue #922, PR 2 slice).
#
# exec_with_secrets runs the caller's command with ONLY the requested values
# injected, as a job the broker tracks by id so start/status/cancel survive
# an MCP adapter reconnect. The actual child always runs as the unprivileged
# agent user - the Launcher below is what decides HOW: production launches a
# systemd transient unit with User=<agent> (SystemdLauncher); tests and a
# root-less dev box use DirectLauncher, which spawns the same
# secret-worker.py directly. Business logic (idempotency, timeouts,
# redaction, output caps) lives here once and is identical either way - only
# "how is the child's uid actually dropped" differs, and that is exactly the
# one thing a unit test on this box cannot prove (see tests/secrets.nix).
#
# Spliced ABOVE secret-broker.py by the generated module, after
# secret-protocol.py; this file therefore has no `if __name__` block and
# reuses SecretError, SECRET_MAX_OUTPUT_BYTES, etc. from that file.
import contextlib
import hashlib
import json
import os
import secrets as _secrets_module
import signal
import subprocess
import threading
import time

JOB_STATES = ("pending", "running", "done", "error", "cancelled", "timeout")
ACTIVE_STATES = ("pending", "running")


def new_job_id():
    return _secrets_module.token_hex(12)


def redact(data, values):
    """Replace every exact occurrence of a secret value with a fixed-width
    marker. Order matters: redact longer values first so one value that is a
    substring of another cannot partially unmask it."""
    if not values:
        return data
    out = data
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            out = out.replace(value.encode("utf-8"), b"***redacted***")
    return out


def fingerprint_request(req):
    """What makes two exec requests "the same" for idempotency: everything
    that affects what runs, nothing that affects bookkeeping (jobId,
    idempotencyKey itself)."""
    material = {
        "profile": req.get("profile"),
        "argv": req.get("argv"),
        "command": req.get("command"),
        "shell": req.get("shell"),
        "secretNames": sorted(req.get("secretNames", [])),
        "cwd": req.get("cwd"),
        "env": req.get("env", {}),
        "timeoutMs": req.get("timeoutMs"),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Job:
    def __init__(self, job_id, namespace, fingerprint, request, created_at):
        self.id = job_id
        self.namespace = namespace
        self.fingerprint = fingerprint
        self.idempotency_key = request.get("idempotencyKey")
        self.timeout_ms = request.get("timeoutMs") or SECRET_DEFAULT_TIMEOUT_MS
        self.created_at = created_at
        self.started_at = None
        self.finished_at = None
        self.state = "pending"
        self.exit_code = None
        self.stdout = b""
        self.stderr = b""
        self.stdout_truncated = False
        self.stderr_truncated = False
        self.handle = None  # set by the Launcher; opaque to everything else
        self.lock = threading.Lock()

    def public(self):
        with self.lock:
            return {
                "jobId": self.id,
                "state": self.state,
                "exitCode": self.exit_code,
                "stdout": self.stdout.decode("utf-8", "replace"),
                "stderr": self.stderr.decode("utf-8", "replace"),
                "stdoutTruncated": self.stdout_truncated,
                "stderrTruncated": self.stderr_truncated,
            }

def encode_worker_frame(argv_or_command, shell, secret_values, nonsecret_env, cwd):
    """The one place that builds secret-worker.py's stdin frame, so a field
    added to it cannot land in one Launcher and not the other - the two
    launchers differ in how the child's uid is dropped, nothing about the
    request it is handed."""
    frame = json.dumps({
        "argv": argv_or_command if shell is None else None,
        "command": argv_or_command if shell is not None else None,
        "shell": shell,
        "secrets": secret_values,
        "env": nonsecret_env,
        "cwd": cwd,
    }).encode("utf-8")
    return len(frame).to_bytes(4, "big") + frame


def spawn_worker(argv, frame_bytes):
    """subprocess.Popen plus the frame handoff, shared by both launchers.
    Raises on any failure to start or to hand off the frame - the caller
    (JobManager._dispatch) is what turns that into a job error rather than
    an unhandled exception."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        proc.stdin.write(frame_bytes)
        proc.stdin.close()
    except OSError:
        proc.kill()
        proc.wait()
        raise
    return proc


class Launcher:
    """How a job's child actually starts. Subclasses own the uid drop."""

    def start(self, job, argv_or_command, shell, secret_values, nonsecret_env, cwd):
        raise NotImplementedError

    def poll(self, job):
        raise NotImplementedError

    def kill(self, job):
        raise NotImplementedError


class DirectLauncher(Launcher):
    """Dev/test launcher: runs secret-worker.py directly, as whatever user
    this broker process itself runs as. This is what every unit test and a
    root-less dev-mode broker use - it proves the protocol, the redaction,
    the timeouts and the idempotency, and nothing about the uid boundary.
    Production (SystemdLauncher, in secret-broker.py) is the only launcher
    that proves that; see the module's VM test."""

    def __init__(self, worker_path, python_bin="python3"):
        self.worker_path = worker_path
        self.python_bin = python_bin

    def start(self, job, argv_or_command, shell, secret_values, nonsecret_env, cwd):
        frame_bytes = encode_worker_frame(argv_or_command, shell, secret_values,
                                          nonsecret_env, cwd)
        job.handle = spawn_worker([self.python_bin, self.worker_path], frame_bytes)
        return job.handle

    def poll(self, job):
        return job.handle.poll()

    def kill(self, job):
        if job.handle is None:
            # _dispatch only calls kill() through cancel()/the timeout path,
            # both of which happen after a successful start() set this - but
            # guard it anyway, since "no handle" must never raise past here.
            return
        try:
            os.killpg(job.handle.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(ProcessLookupError, OSError):
                job.handle.kill()


class JobManager:
    """One per (broker process, user). Tracks every job across every profile
    of that user - the "2 active jobs per user" limit in the issue is a
    per-user limit, not per-profile."""

    def __init__(self, launcher, max_output_bytes, max_jobs):
        self.launcher = launcher
        self.max_output_bytes = max_output_bytes
        self.max_jobs = max_jobs
        self._jobs = {}
        self._by_idempotency = {}  # (namespace, key) -> job id
        self._lock = threading.Lock()

    def _active_count(self):
        return sum(1 for j in self._jobs.values() if j.state in ACTIVE_STATES)

    def start(self, namespace, req, resolve_secret):
        fingerprint = fingerprint_request(req)
        key = req.get("idempotencyKey")
        with self._lock:
            if key is not None:
                existing_id = self._by_idempotency.get((namespace, key))
                if existing_id is not None:
                    existing = self._jobs[existing_id]
                    if existing.fingerprint != fingerprint:
                        raise SecretError("idempotency key conflict")
                    return existing.id
            if self._active_count() >= self.max_jobs:
                raise SecretError("too many active jobs")
            job = Job(new_job_id(), namespace, fingerprint, req, time.time())
            self._jobs[job.id] = job
            if key is not None:
                self._by_idempotency[(namespace, key)] = job.id
        self._dispatch(job, req, resolve_secret)
        return job.id

    def _dispatch(self, job, req, resolve_secret):
        names = req.get("secretNames", [])
        secret_values = {}
        for name in names:
            try:
                value = resolve_secret(name)
            except SecretError as exc:
                value = None
                failure = str(exc)
            else:
                failure = f"no such secret: {name}"
            if value is None:
                job.state = "error"
                job.stderr = failure.encode("utf-8")
                job.finished_at = time.time()
                return
            secret_values[name] = value
        argv_or_command = req["argv"] if "argv" in req else req["command"]
        shell = req.get("shell")
        job.started_at = time.time()
        job.state = "running"
        try:
            self.launcher.start(job, argv_or_command, shell, secret_values,
                                req.get("env", {}), req.get("cwd"))
        except Exception as exc:
            # A job that failed to even START (systemd-run missing, a fork
            # failure, a frame too large for the pipe) must not stay
            # "running" forever: that leaks one of this user's 2 job slots
            # permanently, and a later cancel()/kill() on a job with no
            # handle would crash the broker (see Launcher.kill below).
            job.state = "error"
            job.stderr = f"failed to start: {exc}".encode("utf-8")
            job.finished_at = time.time()
            return
        redact_values = list(secret_values.values())
        threading.Thread(target=self._drain, args=(job, redact_values), daemon=True).start()

    def _drain(self, job, redact_values):
        proc = job.handle
        timeout = (job.timeout_ms / 1000.0) if job.timeout_ms else None
        deadline = (time.time() + timeout) if timeout else None
        # A secret can straddle two 4 KiB reads, so the redaction scan has
        # to see a value whole to catch it - which means not committing the
        # tail of what has been read so far until it either resolves into a
        # redacted match or there is nothing more that could still extend
        # it into one. The simplest way to get that right is to never hold
        # back a PARTIAL raw buffer at all: keep the whole raw buffer (it is
        # cheap, because it is capped) and redact it afresh each time, so a
        # match that only completes on a later read is still found whole
        # the next time this runs. Capping it is what keeps this bounded
        # instead of the unbounded "keep every byte ever produced" the
        # first cut of this function did: `yes` with a 1-hour timeout no
        # longer grows this process's memory without limit, nor does the
        # redaction scan run against more than this many bytes at a time.
        longest_value = max((len(v.encode("utf-8")) for v in redact_values), default=0)
        raw_cap = self.max_output_bytes + longest_value

        def pump(stream, attr):
            raw = bytearray()
            truncated = False
            while True:
                # Keep reading even once the cap below is hit, so a child
                # that fills its pipe buffer never blocks on a reader that
                # has stopped reading; the extra bytes are simply discarded.
                chunk = stream.read(4096)
                if not chunk:
                    break
                room = raw_cap - len(raw)
                if room <= 0:
                    truncated = True
                    with job.lock:
                        setattr(job, attr + "_truncated", True)
                    continue
                if len(chunk) > room:
                    raw.extend(chunk[:room])
                    truncated = True
                else:
                    raw.extend(chunk)
                clean = redact(bytes(raw), redact_values)
                if len(clean) > self.max_output_bytes:
                    clean = clean[:self.max_output_bytes]
                    truncated = True
                with job.lock:
                    setattr(job, attr, clean)
                    setattr(job, attr + "_truncated", truncated)

        out_thread = threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True)
        err_thread = threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)
        out_thread.start()
        err_thread.start()
        timed_out = False
        while True:
            rc = self.launcher.poll(job)
            if rc is not None:
                break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                self.launcher.kill(job)
                break
            time.sleep(0.05)
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        with job.lock:
            job.finished_at = time.time()
            if timed_out:
                job.state = "timeout"
                job.exit_code = None
            elif job.state == "running":
                rc = self.launcher.poll(job)
                job.exit_code = rc
                job.state = "done" if rc == 0 else "error"

    def status(self, namespace, job_id):
        job = self._jobs.get(job_id)
        if job is None or job.namespace != namespace:
            raise SecretError("no such job")
        return job.public()

    def cancel(self, namespace, job_id):
        job = self._jobs.get(job_id)
        if job is None or job.namespace != namespace:
            raise SecretError("no such job")
        with job.lock:
            already_terminal = job.state not in ACTIVE_STATES
        if already_terminal:
            return job.public()
        self.launcher.kill(job)
        with job.lock:
            if job.state == "running":
                job.state = "cancelled"
                job.finished_at = time.time()
        return job.public()

    def sweep(self, retention_seconds):
        """Drop finished jobs older than the retention window, so memory and
        the idempotency map do not grow without bound (issue #922: "24-hour
        result retention")."""
        now = time.time()
        with self._lock:
            stale = [
                jid for jid, job in self._jobs.items()
                if job.state not in ACTIVE_STATES
                and job.finished_at is not None
                and now - job.finished_at > retention_seconds
            ]
            for jid in stale:
                job = self._jobs.pop(jid)
                if job.idempotency_key is not None:
                    self._by_idempotency.pop((job.namespace, job.idempotency_key), None)
