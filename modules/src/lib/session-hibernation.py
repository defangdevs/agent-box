# Conservative harness quiescence for capacity-pressure hibernation (#858).
#
# This file is concatenated after session-capacity.py in the capacity helper.
# It is intentionally read-only: session-cli.sh owns the registry lock, the
# stopped transition and the tmux kill. The helper only answers whether one
# resident runtime can be reconstructed exactly from its native transcript.
import hashlib as hibernate_hashlib
import json as hibernate_json
import os as hibernate_os
from pathlib import Path as HibernatePath
import re as hibernate_re
import time as hibernate_time


HIBERNATE_IDLE_S = 30
HIBERNATE_TAIL_LINES = 400
HIBERNATE_TAIL_BYTES = 2 * 1024 * 1024
HIBERNATE_CODEX_HEAD_BYTES = 64 * 1024
HIBERNATE_CODEX_SCAN_MAX = 400
HIBERNATE_UUID_RE = hibernate_re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class SessionHibernationError(Exception):
    pass


def _hibernate_home():
    return HibernatePath(hibernate_os.environ.get("HOME", ""))


def _hibernate_json_file(path, *, missing=None, max_bytes=2 * 1024 * 1024):
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError:
        return missing
    except OSError as exc:
        raise SessionHibernationError("cannot read %s: %s" % (path, exc)) from exc
    if len(raw) > max_bytes:
        raise SessionHibernationError("state file is too large: %s" % path)
    try:
        return hibernate_json.loads(raw)
    except (UnicodeDecodeError, hibernate_json.JSONDecodeError) as exc:
        raise SessionHibernationError("invalid JSON in %s" % path) from exc


def _hibernate_uuid(value):
    return value if isinstance(value, str) and HIBERNATE_UUID_RE.fullmatch(value) else None


def _hibernate_hash(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise SessionHibernationError("cannot read %s: %s" % (path, exc)) from exc
    if len(raw) > 2 * 1024 * 1024:
        raise SessionHibernationError("state file is too large: %s" % path)
    return hibernate_hashlib.sha256(raw).hexdigest()


def _hibernate_tail(path):
    """Return bounded trailing JSONL lines without walking a large transcript."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, hibernate_os.SEEK_END)
            end = handle.tell()
            pos = end
            chunks = []
            newlines = 0
            while pos > 0 and newlines <= HIBERNATE_TAIL_LINES:
                size = min(65536, pos, HIBERNATE_TAIL_BYTES - sum(map(len, chunks)))
                if size <= 0:
                    break
                pos -= size
                handle.seek(pos)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError as exc:
        raise SessionHibernationError("cannot read transcript %s: %s" % (path, exc)) from exc
    raw = b"".join(reversed(chunks))
    return raw.splitlines()[-HIBERNATE_TAIL_LINES:]


def _hibernate_records(path):
    lines = _hibernate_tail(path)
    if not lines:
        return None
    records = []
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            record = hibernate_json.loads(raw)
        except (UnicodeDecodeError, hibernate_json.JSONDecodeError):
            # A killed writer can leave a partial final record. Unknown means
            # active; never park on evidence we could not parse.
            if index == len(lines) - 1:
                return None
            continue
        if not isinstance(record, dict):
            return None
        records.append(record)
    return records or None


def _hibernate_transcript_state(path, harness):
    records = _hibernate_records(path)
    if records is None:
        return None
    if harness == "claude":
        relevant = [record for record in records
                    if record.get("type") in ("assistant", "user")
                    and isinstance(record.get("message"), dict)]
        if not relevant:
            return None
        last = relevant[-1]
        closed = (last.get("type") == "assistant"
                  and last["message"].get("stop_reason") == "end_turn")
        marker = [last.get("type"), last["message"].get("stop_reason")]
    elif harness == "codex":
        relevant = [(index, record) for index, record in enumerate(records)
                    if record.get("type") == "event_msg"
                    and isinstance(record.get("payload"), dict)
                    and record["payload"].get("type")
                    in ("task_started", "task_complete")]
        if not relevant:
            return None
        last_index, last = relevant[-1]
        closed = last["payload"].get("type") == "task_complete"
        marker = ["event_msg", last["payload"].get("type")]
        # Codex normally appends thread_settings_applied while sitting at the
        # prompt. Anything else after task_complete is an unrecognised state
        # transition and cannot prove quiescence. In particular, do not let a
        # new event type silently turn an active future Codex into a candidate.
        if closed:
            for record in records[last_index + 1:]:
                if (record.get("type") == "event_msg"
                        and isinstance(record.get("payload"), dict)
                        and record["payload"].get("type")
                        == "thread_settings_applied"):
                    continue
                return None
    else:
        return None
    if not closed:
        return None
    try:
        stat = path.stat()
    except OSError as exc:
        raise SessionHibernationError("cannot stat transcript %s: %s" % (path, exc)) from exc
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "marker": marker,
    }


def _hibernate_state(name):
    path = _hibernate_home() / ".local/state/agent-box/session" / (name + ".json")
    value = _hibernate_json_file(path, missing=None)
    if value is None:
        return {}, path
    if not isinstance(value, dict):
        raise SessionHibernationError("invalid session state: %s" % path)
    return value, path


def _hibernate_claude_transcript(name, entry):
    state, state_path = _hibernate_state(name)
    launch = _hibernate_uuid(state.get("launchSessionId"))
    if launch is None and entry.get("hasRun") is True:
        launch = _hibernate_uuid(entry.get("boxSessionId"))
    if launch is None:
        return None
    # The rotation hook keys the first hop by the stable launch ID, even after
    # nativeSessionId has been persisted. Read that record first: otherwise a
    # stale but completed segment can hide the active segment Claude moved to.
    try:
        initial = (_hibernate_home() / ".local/state/agent-box/live-session-id"
                   / launch).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        initial = None
    except (OSError, UnicodeDecodeError):
        return None
    if initial is not None and _hibernate_uuid(initial) is None:
        return None
    native = (_hibernate_uuid(initial)
              or _hibernate_uuid(state.get("nativeSessionId")) or launch)
    # Claude can rotate its UUID while the process stays up. The SessionStart
    # hook records that hop by launch ID; follow a bounded chain so parking
    # preserves the segment the TUI is actually displaying.
    seen = set()
    for _ in range(8):
        if native in seen:
            return None
        seen.add(native)
        live_path = (_hibernate_home() / ".local/state/agent-box/live-session-id"
                     / native)
        try:
            value = live_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            break
        except (OSError, UnicodeDecodeError):
            return None
        rotated = _hibernate_uuid(value)
        if rotated is None or rotated == native:
            break
        native = rotated
    root = _hibernate_home() / ".claude/projects"
    matches = list(root.glob("*/%s.jsonl" % native))
    if not matches:
        matches = list(root.rglob("%s.jsonl" % native))[:2]
    if len(matches) != 1:
        return None
    return native, matches[0], state_path


def _hibernate_codex_home(entry):
    value = hibernate_os.environ.get("CODEX_HOME")
    envstore = hibernate_os.environ.get("AGENT_BOX_ENVSTORE_BIN")
    if envstore:
        import subprocess as hibernate_subprocess
        calls = [[envstore, "get", "CODEX_HOME"]]
        profile = entry.get("profile")
        if isinstance(profile, str) and hibernate_re.fullmatch(r"[A-Za-z0-9_-]+", profile):
            calls.append([envstore, "get", "CODEX_HOME", "--profile", profile])
        for call in calls:
            try:
                proc = hibernate_subprocess.run(call, capture_output=True, text=True,
                                                timeout=5, check=False)
            except (OSError, hibernate_subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0 and proc.stdout:
                value = proc.stdout.rstrip("\n")
    return HibernatePath(value) if value else _hibernate_home() / ".codex"


def _hibernate_codex_exact(root, native):
    matches = list((root / "sessions").glob("*/*/*/rollout-*-%s.jsonl" % native))
    return matches[0] if len(matches) == 1 else None


def _hibernate_codex_transcript(name, entry):
    state, state_path = _hibernate_state(name)
    launch = _hibernate_uuid(state.get("launchSessionId"))
    if launch is None and entry.get("hasRun") is True:
        launch = _hibernate_uuid(entry.get("boxSessionId"))
    if launch is None:
        return None
    root = _hibernate_codex_home(entry)
    # A resumed Codex thread can produce a newer rollout carrying the same
    # stable Agentbox launch marker. Resolve that first; the persisted native
    # pointer is the fallback for prompt-less sessions whose rollout has no
    # marker at all.
    marker = ("agent-box session %s" % launch).encode("utf-8")
    try:
        rollouts = sorted(
            ((candidate.stat().st_mtime_ns, candidate)
             for candidate in (root / "sessions").glob("*/*/*/rollout-*.jsonl")),
            key=lambda item: item[0], reverse=True)[:HIBERNATE_CODEX_SCAN_MAX]
        path = None
        for _mtime, candidate in rollouts:
            with candidate.open("rb") as handle:
                if marker in handle.read(HIBERNATE_CODEX_HEAD_BYTES):
                    path = candidate
                    break
    except OSError:
        return None
    native = _hibernate_uuid(path.stem[-36:]) if path else None
    if path is None:
        native = _hibernate_uuid(state.get("nativeSessionId"))
        path = _hibernate_codex_exact(root, native) if native else None
    if path is None:
        registered = _hibernate_json_file(
            _hibernate_home() / ".local/state/local-whatsapp/codex-threads.json",
            missing={})
        record = registered.get(name) if isinstance(registered, dict) else None
        native = _hibernate_uuid(record.get("thread")) if isinstance(record, dict) else None
        path = _hibernate_codex_exact(root, native) if native else None
    if native is None or path is None:
        return None
    return native, path, state_path


def _hibernate_has_watch(name):
    user = hibernate_os.environ.get("USER") or ""
    state_dir = HibernatePath(hibernate_os.environ.get(
        "LOCAL_WEBHOOK_STATE_DIR",
        str(_hibernate_home() / ".local/state/local-webhook")))
    path = state_dir / ("filter.%s-%s.json" % (user, name))
    value = _hibernate_json_file(path, missing=None)
    if value is None:
        return False, path, "missing"
    if not isinstance(value, dict):
        return True, path, _hibernate_hash(path)
    topics = value.get("topics")
    if not isinstance(topics, list):
        return True, path, _hibernate_hash(path)
    enabled = value.get("enabled") is not False
    return enabled and bool(topics), path, _hibernate_hash(path)


def _hibernate_has_lease(name):
    path = _hibernate_home() / ".local/state/agent-box/lease" / (name + ".json")
    value = _hibernate_json_file(path, missing=None)
    if value is None:
        return False, path, "missing"
    # Invalid or unknown lease state is outstanding, never permission to park.
    pending = not isinstance(value, dict) or value.get("outcome") is None
    return pending, path, _hibernate_hash(path)


def _hibernate_has_whatsapp_delivery(name):
    root = _hibernate_home() / ".local/state/local-whatsapp"
    target_path = root / "target.json"
    target = _hibernate_json_file(target_path, missing=None)
    if target is None:
        return False, {
            "target": "missing",
            "messages": "not-selected",
        }
    if not isinstance(target, dict):
        return True, {
            "target": _hibernate_hash(target_path),
            "messages": "unknown-target",
        }
    if target.get("session") != name:
        return False, {
            "target": _hibernate_hash(target_path),
            "messages": "not-selected",
        }
    messages_path = root / "messages.json"
    state = _hibernate_json_file(messages_path, missing={"messages": {}})
    if not isinstance(state, dict) or not isinstance(state.get("messages"), dict):
        return True, {"target": _hibernate_hash(target_path),
                      "messages": _hibernate_hash(messages_path)}
    pending = any(isinstance(message, dict)
                  and not message.get("control")
                  and not message.get("reply")
                  for message in state["messages"].values())
    return pending, {"target": _hibernate_hash(target_path),
                     "messages": _hibernate_hash(messages_path)}


def _hibernate_crashed(entry):
    value = entry.get("died")
    return ((isinstance(value, int) and not isinstance(value, bool))
            or value is True)


def _hibernate_idle_seconds():
    raw = hibernate_os.environ.get("AGENT_BOX_HIBERNATE_IDLE_S",
                                   str(HIBERNATE_IDLE_S))
    if not raw.isascii() or not raw.isdecimal():
        raise SessionHibernationError(
            "AGENT_BOX_HIBERNATE_IDLE_S must be a non-negative integer")
    return int(raw)


def hibernation_probe(name, entry, live, now=None):
    if not isinstance(entry, dict) or name not in live:
        return None
    harness = entry.get("agent")
    if harness not in ("claude", "codex"):
        return None
    if (entry.get("stopped") is True or entry.get("ephemeral") is True
            or entry.get("origin") == "webhook" or name.startswith("hook-")
            or _hibernate_crashed(entry)):
        return None
    # These panes own a daemon, not one resumable conversation. Recognize
    # both legacy harness-specific encodings as well as the canonical bit.
    if (entry.get("remoteControlDaemon") is True
            or (harness == "codex" and entry.get("remoteControl") is True)
            or (harness == "claude"
                and entry.get("remoteControlServer") is True)):
        return None
    lease, lease_path, lease_hash = _hibernate_has_lease(name)
    watching, watch_path, watch_hash = _hibernate_has_watch(name)
    pending, whatsapp_state = _hibernate_has_whatsapp_delivery(name)
    if lease or watching or pending:
        return None
    target = (_hibernate_claude_transcript(name, entry) if harness == "claude"
              else _hibernate_codex_transcript(name, entry))
    if target is None:
        return None
    native, transcript, state_path = target
    transcript_state = _hibernate_transcript_state(transcript, harness)
    if transcript_state is None:
        return None
    now = hibernate_time.time() if now is None else now
    idle_since = transcript_state["mtimeNs"] / 1_000_000_000
    if now - idle_since < _hibernate_idle_seconds():
        return None
    evidence = {
        "name": name,
        "entry": entry,
        "harness": harness,
        "nativeSessionId": native,
        "transcript": transcript_state,
        "sessionState": _hibernate_hash(state_path),
        "lease": [str(lease_path), lease_hash],
        "watch": [str(watch_path), watch_hash],
        "whatsapp": whatsapp_state,
    }
    token = hibernate_hashlib.sha256(
        hibernate_json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return {
        "name": name,
        "harness": harness,
        "nativeSessionId": native,
        "idleSince": idle_since,
        "token": token,
    }


def hibernation_candidate(sessions, excludes=(), name=None, token=None):
    assumed = hibernate_os.environ.get("AGENT_BOX_HIBERNATE_ASSUME_LIVE")
    if name is not None and assumed == name:
        # The session CLI took the live snapshot before its registry lock.
        # Rechecking the transcript and routing state under that lock is the
        # important fence; if the pane vanished meanwhile, kill_session's
        # already-gone success still means a slot is free.
        live = {name}
    else:
        try:
            live = capacity_live_checked()
        except SessionCapacityError as exc:
            raise SessionHibernationError(str(exc)) from exc
    excluded = set(excludes)
    if name is not None:
        entry = sessions.get(name)
        result = hibernation_probe(name, entry, live)
        if result is None or (token is not None and result["token"] != token):
            return None
        return result
    candidates = []
    for candidate_name, entry in sessions.items():
        if candidate_name in excluded:
            continue
        result = hibernation_probe(candidate_name, entry, live)
        if result is not None:
            candidates.append(result)
    if not candidates:
        return None
    return min(candidates, key=lambda result: (result["idleSince"], result["name"]))
