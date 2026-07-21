#!/usr/bin/env python3
"""Issue command channel: reads the workflow's issue-opened event payload,
extracts the command text, and runs it through agent.py's parse ->
validate pipeline. This step only reads and validates -- no comment, no
close, no apply (that lands in the next step once parsing/validation
itself is verified). Issue text is untrusted input like email body text
(see agent.py's parse_command); the owner-only trigger guard lives in
the workflow (§8), not here.
"""

import json
import os

import agent
import check

MAX_CHARS = 2000


def load_event():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        return json.load(f)


def extract_command_text(event, max_chars=MAX_CHARS):
    """Issue title + body, capped. Same untrusted-input treatment as the
    email channel's body extraction."""
    issue = event["issue"]
    text = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
    return text[:max_chars]


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("missing required environment variable: GEMINI_API_KEY")

    event = load_event()
    text = extract_command_text(event)

    config = check.load_json(check.CONFIG_PATH, None)
    state = check.load_json(check.STATE_PATH, {})
    allowlist = agent.load_allowlist()
    model = config.get("gemini_model", "gemini-3.5-flash")

    verdict, changes, reply_text = agent.handle_command(
        text, config, state, allowlist, model, os.environ["GEMINI_API_KEY"])

    issue_number = event["issue"]["number"]
    print("issue #%s: validated action=%s" % (issue_number, verdict))
    print("would apply:", changes)
    print("would reply:", reply_text)


if __name__ == "__main__":
    main()
