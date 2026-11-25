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
# Claude record types that carry or announce input. One of these after the
# last closed turn means the session is not quiet.
HIBERNATE_CLAUDE_INPUT_TYPES = ("user", "assistant", "attachment", "queue-operation")
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
        relevant = [(index, record) for index, record in enumerate(records)
                    if record.get("type") in ("assistant", "user")
                    and isinstance(record.get("message"), dict)]
        if not relevant:
            return None
        last_index, last = relevant[-1]
        closed = (last.get("type") == "assistant"
                  and last["message"].get("stop_reason") == "end_turn")
        marker = [last.get("type"), last["message"].get("stop_reason")]
        # Claude Code also appends records that are not conversation:
        # last-prompt, cost-state, file-history-snapshot, ai-title and more,
        # some of them when the process EXITS (2.1.322). Those must not read
        # as new input, or the check after the kill always fails and every
        # hibernation is undone and resumed. Input is a user or assistant
        # record (above), or an attachment or queue record that comes with
        # one; any of those after the last turn record is a change.
        if any(record.get("type") in HIBERNATE_CLAUDE_INPUT_TYPES
               for record in records[last_index + 1:]):
            return None
        conversation = hibernate_hashlib.sha256(hibernate_json.dumps(
            last, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")).hexdigest()
    elif harness == "codex":
        relevant = [(index, record) for index, record in enumerate(records)
                    if record.get("type") == "event_msg"
                    and isinstance(record.get("payload"), dict)
                    and record["payload"].get("type")
                    in ("task_started", "task_complete")]
        if not relevant:
            return None
        last_index, last = relevant[-1]
        conversation = None
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
        # What the quiet check compares instead of size and mtime, when the
        # harness writes records after its last turn that are not input.
        "conversation": conversation,
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
    # SessionStart is authoritative for prompt-less sessions and /new.
    # Never park an older completed segment while its replacement is active.
    try:
        live = (_hibernate_home() / ".local/state/agent-box/live-session-id"
                / launch).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        live = None
    except (OSError, UnicodeDecodeError):
        return None
    if live is not None:
        native = _hibernate_uuid(live)
        path = _hibernate_codex_exact(root, native) if native else None
        return (native, path, state_path) if path else None
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


def _hibernate_hours(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 and value == value else None


def _hibernate_topic_expired(topic, default_ttl, now):
    """local-webhook's entry_expired(), and nothing looser.

    The clock runs from subscribedAt only; a per-entry ttlHours beats the
    file's, and 0 means pinned. Anything this cannot prove expired (a topic
    that is not an object, no or unparseable subscribedAt, a ttl that is not
    a number) is live: unknown never permits parking.
    """
    if not isinstance(topic, dict):
        return False
    ttl = topic.get("ttlHours")
    ttl = _hibernate_hours(default_ttl if ttl is None else ttl)
    if not ttl:
        return False
    stamp = topic.get("subscribedAt")
    if not isinstance(stamp, str) or not stamp:
        return False
    import datetime as hibernate_datetime
    try:
        started = hibernate_datetime.datetime.fromisoformat(
            stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is None:
        return False
    return now - started.timestamp() > ttl * 3600


def _hibernate_has_watch(name, now=None):
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
    now = hibernate_time.time() if now is None else now
    # inbox:<name> is the message router's delivery address for this session.
    # The router wakes the session before every delivery and the bus spools
    # the message while it starts, so that subscription alone is no reason to
    # keep the runtime resident. Anything else, or an unreadable entry, is.
    # A subscription local-webhook already considers expired no longer routes
    # events (#884); it only lingers in the file until the next write.
    blocking = [topic for topic in topics
                if not (isinstance(topic, dict) and isinstance(topic.get("topic"), str)
                        and topic["topic"].startswith("inbox:")
                        and topic.get("deliverTo", "session") == "session")
                and not _hibernate_topic_expired(topic, value.get("ttlHours"), now)]
    return enabled and bool(blocking), path, _hibernate_hash(path)


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


# Idle hibernation (AGENT_BOX_HIBERNATE_MODE=idle) is the second, opt-in way
# a session is parked: not because a slot is wanted, but because nobody has
# used it for AGENT_BOX_IDLE_HIBERNATE_S seconds. It reuses every fence of the
# pressure path (closed turn, no lease, no live subscription, no pending
# WhatsApp message, exact native identity) and adds the ones an interactive
# session needs: nobody attached to its pane, no recent keystroke, and no
# Remote Control mirror whose app view would vanish.
def _hibernate_idle_mode():
    mode = hibernate_os.environ.get("AGENT_BOX_HIBERNATE_MODE", "")
    if mode not in ("", "idle"):
        raise SessionHibernationError(
            "AGENT_BOX_HIBERNATE_MODE must be empty or idle")
    return mode == "idle"


def _hibernate_idle_threshold():
    raw = hibernate_os.environ.get("AGENT_BOX_IDLE_HIBERNATE_S", "")
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise SessionHibernationError(
            "AGENT_BOX_IDLE_HIBERNATE_S must be a positive integer in idle mode")
    # Never shorter than the pressure path's own grace period.
    return max(int(raw), _hibernate_idle_seconds())


def _hibernate_tmux_state(name):
    """(attached clients, last client activity epoch) of a session's pane.

    None means tmux could not say, which is never permission to park.
    """
    command = [hibernate_os.environ.get("AGENT_BOX_TMUX_BIN", "tmux"),
               "-L", hibernate_os.environ.get("AGENT_BOX_TMUX_SOCKET", "agent-box"),
               "display-message", "-p", "-t", "=" + name + ":",
               "#{session_attached} #{session_activity}"]
    env = dict(hibernate_os.environ)
    if env.get("AGENT_BOX_TMUX_TMPDIR"):
        env["TMUX_TMPDIR"] = env["AGENT_BOX_TMUX_TMPDIR"]
    import subprocess as hibernate_subprocess
    try:
        proc = hibernate_subprocess.run(command, capture_output=True, text=True,
                                        timeout=5, check=False, env=env)
    except (OSError, hibernate_subprocess.TimeoutExpired):
        return None
    fields = proc.stdout.split()
    if proc.returncode != 0 or len(fields) != 2 or not all(
            field.isascii() and field.isdecimal() for field in fields):
        return None
    return int(fields[0]), int(fields[1])


def hibernation_probe(name, entry, live, now=None, parked=False, idle=False,
                      tmux_state=None):
    # parked=True re-probes a session hibernate_one already marked stopped,
    # so our own marker must not veto it. idle=True widens the pool from
    # hooks to every Claude/Codex session and swaps the pressure grace for
    # the idle threshold; see _hibernate_idle_mode.
    if not isinstance(entry, dict) or name not in live:
        return None
    harness = entry.get("agent")
    if harness not in ("claude", "codex"):
        return None
    if ((not idle and not capacity_is_hook(name, entry))
            or (entry.get("stopped") is True and not parked)
            or _hibernate_crashed(entry)):
        return None
    if idle and entry.get("remoteControl") is True:
        # A mirrored Claude TUI or Codex pairing daemon: parking it would
        # drop the conversation out from under the app that shows it.
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
    # A parked re-probe (the quiet checks around the kill) skips the age
    # test: the selection already proved the idle time, and an exiting
    # Claude writes its last records just then, which renews the mtime.
    # The same goes for the pane checks: killing the pane is what changes them.
    if not parked:
        threshold = (_hibernate_idle_threshold() if idle
                     else _hibernate_idle_seconds())
        if now - idle_since < threshold:
            return None
        if idle:
            pane = (tmux_state or _hibernate_tmux_state)(name)
            # Somebody is looking at the pane, or typed into it recently
            # (a half-written prompt is input the transcript cannot see).
            if pane is None or pane[0] > 0 or now - pane[1] < threshold:
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
    # Narrower than the token: only what native input or routing can change.
    # The token also covers the registry entry and session state file, which
    # hibernate_one itself rewrites while parking.
    # For Claude, the transcript part is the file identity and the last turn
    # record, not size and mtime: records that are not input (see
    # _hibernate_transcript_state) may still arrive, for example on exit.
    if transcript_state["conversation"] is not None:
        quiet_transcript = [transcript_state["path"], transcript_state["device"],
                            transcript_state["inode"], transcript_state["conversation"]]
    else:
        quiet_transcript = transcript_state
    quiet = hibernate_hashlib.sha256(hibernate_json.dumps(
        [quiet_transcript, evidence["lease"], evidence["watch"],
         evidence["whatsapp"]], sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return {
        "name": name,
        "harness": harness,
        "nativeSessionId": native,
        "idleSince": idle_since,
        "token": token,
        "quiet": quiet,
    }


def hibernation_candidate(sessions, excludes=(), name=None, token=None,
                          quiet=None):
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
    idle = _hibernate_idle_mode()
    # Configuration errors are for the caller, not something to swallow
    # per session below.
    _hibernate_idle_seconds()
    if idle:
        _hibernate_idle_threshold()
    if name is not None:
        entry = sessions.get(name)
        result = hibernation_probe(name, entry, live, parked=quiet is not None,
                                   idle=idle)
        if result is None:
            return None
        if quiet is not None:
            return result if result["quiet"] == quiet else None
        if token is not None and result["token"] != token:
            return None
        return result
    candidates = []
    for candidate_name, entry in sessions.items():
        if candidate_name in excluded:
            continue
        try:
            result = hibernation_probe(candidate_name, entry, live, idle=idle)
        except SessionHibernationError:
            # One session with unreadable evidence stays resident and must
            # not hide the others (#885). Configuration was validated above.
            continue
        if result is not None:
            candidates.append(result)
    if not candidates:
        return None
    return min(candidates, key=lambda result: (result["idleSince"], result["name"]))


# Archived Remote Control sessions (#940). When the Claude app archives an
# /rc conversation the server closes the bridge socket with code 4090 and the
# TUI writes one informational warning to its transcript, then keeps running.
# Nothing else records it, so the supervisor parks such a session the way a
# clean /exit would. Unknown or reworded evidence leaves the session up.
ARCHIVED_RC_SIGNS = ("(code 4090)", "archived")


def _archived_rc_warning(record):
    if (record.get("type") != "system"
            or record.get("subtype") != "informational"):
        return False
    content = record.get("content")
    return (isinstance(content, str)
            and all(sign in content for sign in ARCHIVED_RC_SIGNS))


def _archived_parse_time(value):
    if not isinstance(value, str):
        return None
    import datetime as archived_datetime
    try:
        stamp = archived_datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.timestamp()


def _archived_session_created(name):
    command = [hibernate_os.environ.get("AGENT_BOX_TMUX_BIN", "tmux"),
               "-L", hibernate_os.environ.get("AGENT_BOX_TMUX_SOCKET", "agent-box"),
               "display-message", "-p", "-t", "=" + name + ":", "#{session_created}"]
    env = dict(hibernate_os.environ)
    if env.get("AGENT_BOX_TMUX_TMPDIR"):
        env["TMUX_TMPDIR"] = env["AGENT_BOX_TMUX_TMPDIR"]
    import subprocess as archived_subprocess
    try:
        proc = archived_subprocess.run(command, capture_output=True, text=True,
                                       timeout=5, check=False, env=env)
    except (OSError, archived_subprocess.TimeoutExpired):
        return None
    raw = proc.stdout.strip()
    return int(raw) if proc.returncode == 0 and raw.isdecimal() else None


def archived_probe(name, entry, live, created=_archived_session_created):
    if not isinstance(entry, dict) or name not in live:
        return False
    if (entry.get("agent") != "claude" or entry.get("stopped") is True
            or _hibernate_crashed(entry)
            or entry.get("remoteControlDaemon") is True
            or entry.get("remoteControlServer") is True):
        return False
    target = _hibernate_claude_transcript(name, entry)
    if target is None:
        return False
    records = _hibernate_records(target[1])
    if not records:
        return False
    turns = [(i, r) for i, r in enumerate(records)
             if r.get("type") in ("assistant", "user")
             and isinstance(r.get("message"), dict)]
    warned = [i for i, r in enumerate(records) if _archived_rc_warning(r)]
    if not warned:
        return False
    at = warned[-1]
    # No user or assistant record may follow the warning: later input means
    # somebody is using the session, and an open turn is never parked.
    if any(i > at for i, _ in turns):
        return False
    before = [r for i, r in turns if i < at]
    # No proof of a closed turn (none seen, or the last is open) means no park.
    if not before:
        return False
    last = before[-1]
    if not (last.get("type") == "assistant"
            and last["message"].get("stop_reason") == "end_turn"):
        return False
    # A resumed session replays the old transcript tail. Only a warning
    # written by THIS tmux session counts, or restart would re-park at once.
    stamp = _archived_parse_time(records[at].get("timestamp"))
    started = created(name)
    return stamp is not None and started is not None and stamp > started


def archived_sessions(sessions):
    try:
        live = capacity_live_checked()
    except SessionCapacityError as exc:
        raise SessionHibernationError(str(exc)) from exc
    archived = []
    for name, entry in sessions.items():
        try:
            if archived_probe(name, entry, live):
                archived.append(name)
        except (SessionHibernationError, OSError):
            # Unreadable evidence for one session must not block the others.
            continue
    return sorted(archived)


def finish_state(name, entry, since):
    """Whether a session that asked to finish may be removed now.

    `agent-box-session finish` cannot simply delist its own caller: a webhook
    delivery that lands between the agent's last look and the delist sits in
    the harness's input queue, unread, and dies with the pane (PR 1076: five
    CodeRabbit comments arrived 0.1 s before the session removed itself). The
    transcript is the only place that queue is visible, so the waiter asks it.

    Returns one of:
      "idle"    the last turn is closed and nothing is queued behind it
      "busy"    a turn is open, or input is queued that the harness has not
                started on yet: keep waiting, it will be read first
      "input"   something other than a tool result reached the session after
                SINCE (a delivery, or a person typing): the session is no
                longer finished, so the caller must stand down
      "unknown" no transcript can be tied to the session: never remove on
                evidence we do not have

    SINCE is epoch seconds. Codex rollouts carry no queue records, so for
    Codex only an open turn is visible and "input" is never returned.
    """
    if not isinstance(entry, dict):
        return "unknown"
    harness = entry.get("agent")
    if harness == "claude":
        target = _hibernate_claude_transcript(name, entry)
    elif harness == "codex":
        target = _hibernate_codex_transcript(name, entry)
    else:
        return "unknown"
    if target is None:
        return "unknown"
    _native, transcript, _state_path = target
    if harness == "claude":
        records = _hibernate_records(transcript)
        if records is None:
            return "unknown"
        for record in records:
            stamp = _archived_parse_time(record.get("timestamp"))
            if stamp is None or stamp <= since:
                continue
            kind = record.get("type")
            # Only an enqueue adds input; a dequeue or remove of something
            # queued before `since` is the harness draining, not new work.
            if kind == "queue-operation":
                if record.get("operation") == "enqueue":
                    return "input"
                continue
            message = record.get("message")
            if kind == "user" and isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return "input"
                if isinstance(content, list) and any(
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                        for block in content):
                    return "input"
    return "idle" if _hibernate_transcript_state(transcript, harness) else "busy"
