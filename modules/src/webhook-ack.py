# GitHub reaction adapter for local-webhook's post-acceptance hook.
# The env-store library is prepended by both backends. Tokens never ride in
# a webhook or a unit file; read the user's store at invocation time.

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


class AckFailure(Exception):
    pass


def positive_id(value):
    return str(value) if type(value) is int and value > 0 else None


def github_target(envelope):
    if (envelope.get("source") != "github"
            or envelope.get("format") != "github"):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    repo = envelope.get("key")
    if (not isinstance(repo, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or payload.get("repository", {}).get("full_name") != repo):
        return None
    event, action = envelope.get("event"), payload.get("action")
    if event in ("issue_comment", "pull_request_review_comment"):
        if action not in ("created", "edited"):
            return None
        ident = positive_id(payload.get("comment", {}).get("id"))
        if ident:
            kind = "issues" if event == "issue_comment" else "pulls"
            return repo, "%s/comments/%s" % (kind, ident), None
    if event == "pull_request_review" and action == "submitted":
        # GitHub has no reaction endpoint for a review summary. React to
        # its inline comments, selected by review id, never all PR comments.
        review = positive_id(payload.get("review", {}).get("id"))
        number = positive_id(payload.get("pull_request", {}).get("number"))
        if review and number:
            path = "pulls/%s/reviews/%s/comments" % (number, review)
            return repo, path, review
    if event in ("issues", "pull_request") and action in (
            "opened", "reopened", "edited", "assigned", "review_requested"):
        obj = payload.get("issue" if event == "issues" else "pull_request", {})
        number = positive_id(obj.get("number"))
        if number:
            return repo, "issues/%s" % number, None
    return None


def github_token():
    store = as_dict(load(os.path.expanduser("~/.config/agent-box/env")))
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
             or store.get("GH_TOKEN") or store.get("GITHUB_TOKEN"))
    if token:
        return token
    # gh login is also supported; no key is placed on argv or in logs.
    try:
        result = subprocess.run(
            [GH_BIN, "auth", "token", "--hostname", "github.com"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=True)
        return result.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError):
        raise AckFailure("no GitHub token available") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even an API redirect must not forward the credential elsewhere.
        return None


def github_api(token, method, path):
    data = b'{"content":"eyes"}' if method == "POST" else None
    request = urllib.request.Request(
        "https://api.github.com/repos/" + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "agent-box-webhook-ack"})
    try:
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=3) as response:
            return json.load(response)
    except (OSError, ValueError):
        raise AckFailure("GitHub acknowledgment request failed") from None


def acknowledge(envelope, api=None, token=None):
    target = github_target(envelope)
    if target is None:
        return
    repo, path, review = target
    token = token or github_token()
    api = api or github_api
    if review is None:
        # GitHub returns 200 for this account's existing reaction, 201 for
        # a new one. That remote uniqueness makes edits, replays, concurrent
        # workers and token rotation idempotent per account and comment.
        api(token, "POST", "%s/%s/reactions" % (repo, path))
        return
    for page in range(1, 11):
        comments = api(token, "GET", "%s/%s?per_page=100&page=%d"
                       % (repo, path, page))
        if not isinstance(comments, list):
            raise AckFailure("invalid review comments response")
        for comment in comments:
            ident = positive_id(comment.get("id"))
            review_id = positive_id(comment.get("pull_request_review_id"))
            if ident and review_id == review:
                api(token, "POST", "%s/pulls/comments/%s/reactions"
                    % (repo, ident))
        if len(comments) < 100:
            return
    raise AckFailure("review comment pagination limit reached")


def main():
    try:
        record = json.load(sys.stdin)
        envelope = record.get("envelope") if isinstance(record, dict) else None
        if not isinstance(envelope, dict):
            raise AckFailure("invalid acceptance envelope")
        acknowledge(envelope)
        return 0
    except (AckFailure, ValueError, TypeError, AttributeError):
        # Never print payloads, URLs or exception details carrying secrets.
        print("agent-box: webhook acknowledgment failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
