#!/usr/bin/env python3
r"""Tests for the file drop's path confinement (issue #630).

Why this exists
---------------
Caddy used to serve /<user>/downloads/ itself, with `root` + `file_server`.
A site root is not a filesystem sandbox -- caddy says so -- and one caddy
reads every user's drop through its own group, so a symlink an agent dropped
in its OWN drop was followed under that shared identity: a request
authenticated as alice returned bob's file with HTTP 200.

The fix moves the drop into each user's own settings daemon and resolves the
request path with the confinement built INTO the resolution: every component
is opened with O_NOFOLLOW relative to the fd of the directory above it, so no
path string is ever handed back to the kernel to look up a second time. That
is the part worth testing, because the obvious alternative -- realpath() and
then open() -- passes every static test here and still loses the file to an
agent that swaps a symlink in between the two calls. The last test in this
file is the one that tells those two implementations apart.

So this covers both halves: the resolver's own semantics, and the real
handler over HTTP with a thread swapping a symlink underneath it. It needs no
VM and no Nix -- run it directly:

    python3 tests/test-downloads.py

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py, for the reason test-webhook-panel-state.py
gives: the daemon ships with the env-store library prepended, so the source
file alone does not import. The golden payload is that assembled article, and
the golden-snapshot check fails if it stops matching.
"""
import errno
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

USER = "alice"
BASE = "/" + USER + "/downloads"
# What a sibling user's drop holds in these tests: the thing that must never
# come back out of alice's URL space.
SECRET = "bob's private report\n"
SAFE = "alice's own report\n"

WORK = None          # everything this run creates
ROOT = None          # alice's drop: what the daemon is pointed at
OUTSIDE = None       # a sibling drop, deliberately reachable to the process
daemon = None        # the settings daemon, imported with ROOT configured


def setUpModule():
    global WORK, ROOT, OUTSIDE, daemon
    WORK = tempfile.mkdtemp(prefix="agent-box-downloads-")
    ROOT = os.path.join(WORK, "drop-alice")
    OUTSIDE = os.path.join(WORK, "drop-bob")
    os.mkdir(ROOT)
    os.mkdir(OUTSIDE)
    with open(os.path.join(OUTSIDE, "report.txt"), "w") as handle:
        handle.write(SECRET)
    # Imported under exactly the unit's environment, which the daemon reads
    # once at import: AGENT_BOX_DOWNLOADS_DIR is what the drop IS. Nothing
    # of the caller's is kept but PATH -- a GH_TOKEN in the runner's own
    # environment would put the daemon on a different code path than a box
    # takes.
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["HOME"] = WORK
    os.environ["AGENT_BOX_SETTINGS_USER"] = USER
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = os.path.join(WORK, "env")
    os.environ["AGENT_BOX_SESSIONS_FILE"] = os.path.join(
        WORK, "sessions.json")
    os.environ["AGENT_BOX_SETTINGS_BASE"] = "/" + USER + "/settings"
    os.environ["AGENT_BOX_DOWNLOADS_DIR"] = ROOT
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        daemon = importlib.util.module_from_spec(spec)
        loader.exec_module(daemon)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    # One access-log line per request, and the hammer test below makes 300
    # of them: the check's output IS its log, so keep the assertions
    # legible in it.
    daemon.Handler.log_message = lambda self, fmt, *args: None


def tearDownModule():
    shutil.rmtree(WORK, ignore_errors=True)


def drop(name, text=SAFE):
    """Write a file into alice's drop and return its path.

    Written beside the name and rename()d onto it, never opened by name: a
    test that ran earlier may have left that name a symlink pointing OUT of
    the drop, and opening it for writing would follow the link and put
    alice's text in bob's file -- corrupting the very thing the escape
    tests look for."""
    path = os.path.join(ROOT, name)
    spare = path + ".new"
    with open(spare, "w") as handle:
        handle.write(text)
    os.rename(spare, path)
    return path


def link(name, target):
    """Point `name` in alice's drop at `target`, replacing whatever is there
    (the resolver tests and the HTTP tests share one drop)."""
    path = os.path.join(ROOT, name)
    spare = path + ".new"
    os.symlink(target, spare)
    os.rename(spare, path)
    return path


def read_fd(fd, size):
    return os.pread(fd, size, 0).decode()


class ResolveTest(unittest.TestCase):
    """dl_open's own semantics, called the way the handler calls it."""

    def resolve(self, *comps):
        return daemon.dl_open(ROOT, list(comps))

    def test_plain_file(self):
        drop("plain.txt")
        fd, info = self.resolve("plain.txt")
        try:
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)

    def test_nested_file(self):
        os.makedirs(os.path.join(ROOT, "a", "b"), exist_ok=True)
        drop("a/b/deep.txt")
        fd, info = self.resolve("a", "b", "deep.txt")
        try:
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)

    def test_root_itself_is_a_directory(self):
        fd, info = self.resolve()
        try:
            self.assertTrue(stat.S_ISDIR(info.st_mode))
        finally:
            os.close(fd)

    def test_symlink_inside_the_drop_is_followed(self):
        # The reason resolution follows a link at all: `ln -s` into the drop
        # is a reasonable thing for an agent to do, and it stays inside.
        drop("target.txt")
        link("link.txt", "target.txt")
        fd, info = self.resolve("link.txt")
        try:
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)

    def test_absolute_symlink_out_of_the_drop_resolves_to_nothing(self):
        # Issue #630's own evidence, at the resolver: an absolute target is
        # clamped to the drop, so /<work>/drop-bob/report.txt is looked for
        # UNDER the drop, where there is no such tree.
        link("sibling.txt", os.path.join(OUTSIDE, "report.txt"))
        with self.assertRaises(OSError) as caught:
            self.resolve("sibling.txt")
        self.assertEqual(caught.exception.errno, errno.ENOENT)

    def test_relative_symlink_cannot_climb_out(self):
        link("climb.txt", "../drop-bob/report.txt")
        with self.assertRaises(OSError) as caught:
            self.resolve("climb.txt")
        self.assertEqual(caught.exception.errno, errno.ENOENT)

    def test_dotdot_in_the_request_stays_at_the_root(self):
        drop("clamped.txt")
        # ".." at the root is a no-op, so this names the file itself again
        # rather than reaching the drop's parent -- RESOLVE_IN_ROOT's rule.
        fd, info = self.resolve("..", "..", "clamped.txt")
        try:
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)

    def test_dotdot_below_the_root_still_walks_back_up(self):
        os.makedirs(os.path.join(ROOT, "sub"), exist_ok=True)
        drop("up.txt")
        fd, info = self.resolve("sub", "..", "up.txt")
        try:
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)

    def test_symlinked_directory_out_of_the_drop_resolves_to_nothing(self):
        link("bobdir", OUTSIDE)
        with self.assertRaises(OSError) as caught:
            self.resolve("bobdir", "report.txt")
        self.assertEqual(caught.exception.errno, errno.ENOENT)

    def test_symlink_loop_is_refused(self):
        link("loop-a", "loop-b")
        link("loop-b", "loop-a")
        with self.assertRaises(daemon.DownloadRefused):
            self.resolve("loop-a")

    def test_a_file_is_not_a_directory(self):
        drop("leaf.txt")
        with self.assertRaises(daemon.DownloadRefused):
            self.resolve("leaf.txt", "more")

    def test_a_fifo_opens_without_blocking_and_is_not_a_file(self):
        # O_NONBLOCK is what keeps a FIFO in the drop from parking a request
        # thread inside open() forever; the handler then refuses it on mode.
        path = os.path.join(ROOT, "pipe")
        if not os.path.exists(path):
            os.mkfifo(path)
        fd, info = self.resolve("pipe")
        try:
            self.assertTrue(stat.S_ISFIFO(info.st_mode))
            self.assertFalse(stat.S_ISREG(info.st_mode))
        finally:
            os.close(fd)

    def test_the_open_fd_survives_a_symlink_swap(self):
        """The whole point of resolving with fds (issue #630).

        A realpath()-then-open() check passes every test above and fails
        this one: the swap lands between its check and its open, and it
        opens what the attacker substituted. Here the fd already refers to
        the inode the walk reached, so the swap changes nothing about what
        the response body will be."""
        drop("swap.txt")
        fd, info = self.resolve("swap.txt")
        try:
            evil = os.path.join(ROOT, "swap.txt.evil")
            os.symlink(os.path.join(OUTSIDE, "report.txt"), evil)
            os.rename(evil, os.path.join(ROOT, "swap.txt"))
            self.assertEqual(read_fd(fd, info.st_size), SAFE)
        finally:
            os.close(fd)
            drop("swap.txt")


class ComponentTest(unittest.TestCase):
    """dl_components: what the URL path is allowed to say."""

    def test_decodes_after_splitting(self):
        self.assertEqual(daemon.dl_components("/a/b%20c.txt"),
                         ["a", "b c.txt"])

    def test_empty_and_dot_segments_drop_out(self):
        self.assertEqual(daemon.dl_components("//a//./b/"), ["a", "b"])

    def test_encoded_separator_is_refused_not_reinterpreted(self):
        # %2F must never become a path separator, and must not silently
        # become a literal "/" inside a name either.
        self.assertIsNone(daemon.dl_components("/..%2f..%2fetc/passwd"))
        self.assertIsNone(daemon.dl_components("/a%2Fb"))

    def test_nul_is_refused(self):
        self.assertIsNone(daemon.dl_components("/a%00b"))

    def test_dotdot_survives_for_the_resolver_to_clamp(self):
        self.assertEqual(daemon.dl_components("/../x"), ["..", "x"])


class RangeTest(unittest.TestCase):
    """dl_range: what a resumed or seeking client asks for."""

    def test_absent_or_unparseable_means_the_whole_body(self):
        for header in (None, "", "items=0-1", "bytes=", "bytes=x-y",
                       "bytes=0-1,4-5"):
            self.assertIsNone(daemon.dl_range(header, 100), header)

    def test_explicit_span(self):
        self.assertEqual(daemon.dl_range("bytes=10-19", 100), (10, 19))

    def test_open_ended_span_stops_at_the_last_byte(self):
        self.assertEqual(daemon.dl_range("bytes=90-", 100), (90, 99))
        self.assertEqual(daemon.dl_range("bytes=0-999", 100), (0, 99))

    def test_suffix_span(self):
        self.assertEqual(daemon.dl_range("bytes=-10", 100), (90, 99))
        self.assertEqual(daemon.dl_range("bytes=-500", 100), (0, 99))

    def test_unsatisfiable_owes_a_416(self):
        self.assertEqual(daemon.dl_range("bytes=100-", 100), ())
        self.assertEqual(daemon.dl_range("bytes=20-10", 100), ())
        self.assertEqual(daemon.dl_range("bytes=-0", 100), ())
        self.assertEqual(daemon.dl_range("bytes=0-", 0), ())


class ServeTest(unittest.TestCase):
    """The real daemon over HTTP, as caddy reverse-proxies to it."""

    server = None
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), daemon.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, headers=headers or {})
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    def test_a_dropped_file_downloads(self):
        drop("report.txt")
        status, headers, body = self.get(BASE + "/report.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), SAFE)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(headers["Content-Length"], str(len(SAFE)))
        self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_a_name_with_spaces_downloads(self):
        drop("build log v2.txt")
        status, _headers, body = self.get(BASE + "/build%20log%20v2.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), SAFE)

    def test_a_large_file_streams_intact(self):
        # Sixty-odd DL_CHUNKs, so it exercises the write loop rather than
        # a single send, and big enough that a truncating or off-by-a-chunk
        # bug shows in the digest. Not larger: the body is compared in
        # memory, and this check runs on every architecture.
        blob = (b"agent-box download stream test\n" * 200_000)[:4 << 20]
        path = os.path.join(ROOT, "artifact.bin")
        with open(path, "wb") as handle:
            handle.write(blob)
        status, headers, body = self.get(BASE + "/artifact.bin")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(blob)))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(hashlib.sha256(body).hexdigest(),
                         hashlib.sha256(blob).hexdigest())

    def test_a_range_request_resumes(self):
        drop("resume.txt", "0123456789abcdef")
        status, headers, body = self.get(
            BASE + "/resume.txt", headers={"Range": "bytes=4-9"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"456789")
        self.assertEqual(headers["Content-Range"], "bytes 4-9/16")

    def test_an_unsatisfiable_range_is_a_416(self):
        drop("short.txt", "abc")
        status, headers, body = self.get(
            BASE + "/short.txt", headers={"Range": "bytes=99-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */3")
        self.assertEqual(body, b"")

    def test_head_answers_with_the_size_and_no_body(self):
        drop("sized.txt")
        status, headers, body = self.get(BASE + "/sized.txt", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(SAFE)))
        self.assertEqual(body, b"")

    def test_the_index_lists_the_drop(self):
        drop("listed.txt")
        os.makedirs(os.path.join(ROOT, "folder"), exist_ok=True)
        status, headers, body = self.get(BASE + "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        page = body.decode()
        self.assertIn("listed.txt", page)
        self.assertIn('href="folder/"', page)

    def test_the_index_escapes_a_hostile_name(self):
        drop("<b>bold.txt")
        _status, _headers, body = self.get(BASE + "/")
        page = body.decode()
        self.assertNotIn("<b>bold", page)
        self.assertIn("&lt;b&gt;bold", page)

    def test_a_file_is_not_served_at_a_trailing_slash(self):
        # "report.txt/" names nothing: caddy's file_server answered ENOTDIR
        # for it, and the shape matters to issue #631, whose per-file
        # response headers are matched with `not path */` -- a file served
        # at a path ending in "/" would arrive exempt from them.
        drop("slashed.txt")
        status, _headers, body = self.get(BASE + "/slashed.txt/")
        self.assertEqual(status, 404)
        self.assertNotIn(SAFE, body.decode())

    def test_a_dot_component_still_names_the_file(self):
        # Caddy normalizes "/x/." to "/x" before it proxies, but the daemon
        # must not depend on that: the "." drops out of the path and the
        # file answers. 200 is also the RIGHT answer for issue #631, whose
        # per-file headers match `not path */` -- this path does not end in
        # a slash, so it is decorated like any other file.
        drop("dotted.txt")
        status, _headers, body = self.get(BASE + "/dotted.txt/.")
        self.assertEqual((status, body.decode()), (200, SAFE))

    def test_a_directory_redirects_to_its_slash(self):
        os.makedirs(os.path.join(ROOT, "folder"), exist_ok=True)
        status, headers, _body = self.get(BASE + "/folder")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], BASE + "/folder/")

    def test_a_symlink_to_a_sibling_drop_is_refused(self):
        # Issue #630 end to end: this exact request returned bob's marker
        # with HTTP 200 while caddy served the tree.
        link("sibling.txt", os.path.join(OUTSIDE, "report.txt"))
        status, _headers, body = self.get(BASE + "/sibling.txt")
        self.assertEqual(status, 404)
        self.assertNotIn("private report", body.decode())

    def test_a_symlink_to_an_absolute_system_path_is_refused(self):
        link("hostname", "/etc/hostname")
        status, _headers, body = self.get(BASE + "/hostname")
        self.assertEqual(status, 404)
        self.assertNotIn(socket.gethostname(), body.decode())

    def test_an_encoded_traversal_is_refused(self):
        status, _headers, _body = self.get(BASE + "/..%2f..%2fetc/passwd")
        self.assertEqual(status, 404)

    def test_a_traversal_stays_inside_the_drop(self):
        drop("inside.txt")
        status, _headers, body = self.get(BASE + "/../../inside.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), SAFE)

    def test_a_fifo_is_refused_rather_than_hanging(self):
        path = os.path.join(ROOT, "pipe")
        if not os.path.exists(path):
            os.mkfifo(path)
        status, _headers, _body = self.get(BASE + "/pipe")
        self.assertEqual(status, 404)

    def test_both_states_of_one_name_answer_correctly(self):
        """The two ends of the race, pinned without any timing.

        The hammer below asserts an INVARIANT over whatever interleaving it
        happens to get, which on a loaded machine can be all-refusals or
        all-hits. So the two outcomes it is allowed to see are each nailed
        down here first: the same URL, served when the name is a file in
        the drop and refused when it is a link out of it."""
        drop("flip")
        status, _headers, body = self.get(BASE + "/flip")
        self.assertEqual((status, body.decode()), (200, SAFE))
        link("flip", os.path.join(OUTSIDE, "report.txt"))
        status, _headers, body = self.get(BASE + "/flip")
        self.assertEqual(status, 404)
        self.assertNotIn("private report", body.decode())

    def test_a_symlink_swapped_in_mid_request_never_leaks(self):
        """The acceptance criterion a static test cannot state (#630).

        One thread hammers /<user>/downloads/flip while another swaps that
        name between a real in-drop file and a symlink pointing at the
        sibling drop -- atomically, with rename(), so every request sees one
        or the other and never a half-written name. Whatever the timing, a
        response is either the file that was in the drop or a refusal, and
        the sibling's content is in neither.

        Against a realpath()-then-open() resolver this is the test that
        eventually fails, because there the swap has a window to land in:
        the check reads one path and the open reads it again.
        """
        name = os.path.join(ROOT, "flip")
        drop("flip")
        stop = threading.Event()
        errors = []

        def swapper():
            index = 0
            try:
                while not stop.is_set():
                    index += 1
                    spare = "%s.%d" % (name, index)
                    if index % 2:
                        os.symlink(os.path.join(OUTSIDE, "report.txt"), spare)
                    else:
                        with open(spare, "w") as handle:
                            handle.write(SAFE)
                    os.rename(spare, name)
            except OSError as exc:      # pragma: no cover - swapper failure
                errors.append(exc)

        thread = threading.Thread(target=swapper)
        thread.start()
        # Bounded both ways: enough requests to interleave with a thread
        # that swaps as fast as the kernel will rename, and a wall clock so
        # a loaded CI runner cannot turn contention into a long build.
        deadline = time.monotonic() + 15
        try:
            for _ in range(200):
                status, _headers, body = self.get(BASE + "/flip")
                self.assertIn(status, (200, 404), body[:200])
                if status == 200:
                    self.assertEqual(body.decode(), SAFE)
                self.assertNotIn(b"private report", body)
                if time.monotonic() > deadline:
                    break
        finally:
            stop.set()
            thread.join(timeout=30)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
