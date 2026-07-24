#!/usr/bin/env python3
"""Issue command channel: reads the workflow's issue-opened event payload,
runs the command through agent.py's parse -> validate -> apply pipeline,
then posts the result as a comment and closes the issue. Issue text is
untrusted input like email body text (see agent.py's parse_command); the
owner-only trigger guard lives in the workflow (§8), not here.

Comment/close API URLs are built ONLY from the event payload's own repo
fields (full_name) and issue number -- never from anything the model
returned -- the same "code constructs every URL" discipline as check.py's
request URLs.
"""

import json
import os
import urllib.error
import urllib.request

import agent
import check

MAX_CHARS = 2000
GITHUB_API_BASE = "https://api.github.com"


def load_event():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        return json.load(f)


def extract_command_text(event, max_chars=MAX_CHARS):
    """Issue title + body, capped. Same untrusted-input treatment as the
    email channel's body extraction."""
    issue = event["issue"]
    text = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
    return text[:max_chars]


def _github_request(url, github_token, method, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % github_token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "rate-agent/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read(300).decode("utf-8", "replace")
        except OSError:
            pass
        raise RuntimeError("HTTP %d for %s %s: %s" % (err.code, method, url, detail)) from err


def post_comment(repo_full_name, issue_number, body, github_token):
    url = "%s/repos/%s/issues/%s/comments" % (GITHUB_API_BASE, repo_full_name, issue_number)
    return _github_request(url, github_token, "POST", {"body": body})


def close_issue(repo_full_name, issue_number, github_token):
    url = "%s/repos/%s/issues/%s" % (GITHUB_API_BASE, repo_full_name, issue_number)
    return _github_request(url, github_token, "PATCH", {"state": "closed"})


def main():
    for name in ("GEMINI_API_KEY", "GITHUB_TOKEN"):
        if not os.environ.get(name):
            raise SystemExit("missing required environment variable: %s" % name)
    github_token = os.environ["GITHUB_TOKEN"]
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true")

    def scrub(text):
        return text.replace(github_token, "<GITHUB_TOKEN>") if github_token else text

    event = load_event()
    text = extract_command_text(event)
    repo_full_name = event["repository"]["full_name"]
    issue_number = event["issue"]["number"]

    config = check.load_json(check.CONFIG_PATH, None)
    state = check.load_json(check.STATE_PATH, {})
    allowlist = agent.load_allowlist()
    model = config.get("gemini_model", "gemini-3.5-flash")

    try:
        verdict, changes, reply_text = agent.handle_command(
            text, config, state, allowlist, model, os.environ["GEMINI_API_KEY"])
    except agent.GeminiError as err:
        # Transient parse outage: comment so the owner knows, but leave the
        # issue OPEN and change nothing, so it can be retried (open a new
        # issue) rather than silently closed as "couldn't parse".
        print("issue #%s: parse temporarily unavailable (%s)"
              % (issue_number, scrub(str(err))))
        if not dry_run:
            post_comment(repo_full_name, issue_number,
                         "Command parsing is temporarily unavailable; "
                         "please open a new issue to retry.", github_token)
        return
    print("issue #%s: validated action=%s" % (issue_number, verdict))

    if dry_run:
        print("DRY RUN: would apply %r, comment %r, and close issue #%s"
              % (changes, reply_text, issue_number))
        return

    agent.apply_changes(changes, config, state)
    try:
        post_comment(repo_full_name, issue_number, reply_text, github_token)
        close_issue(repo_full_name, issue_number, github_token)
    except RuntimeError as err:
        print("ERROR commenting/closing issue #%s: %s" % (issue_number, scrub(str(err))))

    check.save_json(check.CONFIG_PATH, config)
    check.save_json(check.STATE_PATH, state)


if __name__ == "__main__":
    main()
