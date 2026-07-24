#!/usr/bin/env python3
"""Email command channel: polls the agent's mailbox for candidate command
messages, gates them by sender, subject keyword, and DKIM, then runs
accepted candidates through agent.py's parse -> validate -> apply ->
reply pipeline.

Command text is untrusted input (see agent.py's parse_command) -- the
three gates decide whether a message is even a CANDIDATE for that
pipeline. They do not themselves authorize any config/state change; only
validate_command()'s allowlist/bounds checks decide that. Every message
is marked \\Seen after processing, win or lose, so it is never
reprocessed on the next poll.
"""

import email
import imaplib
import os
import re

import agent
import check
import mailer

REPLY_SUBJECT_PREFIX = "Agent: "


def passes_gates(msg, command_sender, command_keyword):
    """Three gates, ALL required. from_ok/subject_ok alone are NOT
    sufficient -- a spoofed From header still fails dkim_ok, the strong
    gate here. Returns (passed, {"from": bool, "subject": bool, "dkim": bool})."""
    from_header = msg.get("From", "")
    subject_header = msg.get("Subject", "")
    auth_results = msg.get("Authentication-Results", "")

    gates = {
        "from": command_sender in from_header,
        "subject": command_keyword.lower() in subject_header.lower(),
        "dkim": "dkim=pass" in auth_results.lower(),
    }
    return all(gates.values()), gates


def extract_body(msg, max_chars=2000):
    """Prefer the text/plain part; fall back to the raw payload if the
    message isn't multipart. Capped at max_chars -- this text goes to
    Gemini and nowhere else."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, "replace")[:max_chars]
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, "replace")[:max_chars]


def fetch_candidates(imap):
    """Search UNSEEN, return [(msg_id_bytes, email.message.Message), ...].

    Uses BODY.PEEK[] rather than RFC822 to read each message: a plain
    RFC822 fetch implicitly sets \\Seen as a side effect, which would mark
    a message read merely by examining it -- so a message we skip (gate
    reject, or a transient parse outage we want to retry) would silently
    never be seen again, and even a dry run would consume the inbox.
    PEEK fetches without touching the flag; \\Seen is set explicitly by
    the caller only after a message is actually processed."""
    status, data = imap.search(None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    candidates = []
    for msg_id in data[0].split():
        status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
        if status != "OK":
            continue
        candidates.append((msg_id, email.message_from_bytes(msg_data[0][1])))
    return candidates


def build_reply_subject(reply_text, command_keyword):
    """"Agent: " + a short summary, with the command keyword stripped out
    if it ever appeared. Replies go to MAIL_TO, never the polled inbox, so
    this can't actually create a poll loop -- the strip is defense in
    depth per §7, not the only thing preventing one."""
    first_line = reply_text.splitlines()[0] if reply_text else "command result"
    subject = REPLY_SUBJECT_PREFIX + first_line
    if command_keyword:
        subject = re.sub(re.escape(command_keyword), "[keyword]", subject, flags=re.IGNORECASE)
    return subject


def main():
    for name in ("MAIL_USERNAME", "MAIL_APP_PASSWORD", "MAIL_TO",
                 "COMMAND_SENDER", "COMMAND_KEYWORD", "GEMINI_API_KEY"):
        if not os.environ.get(name):
            raise SystemExit("missing required environment variable: %s" % name)

    command_sender = os.environ["COMMAND_SENDER"]
    command_keyword = os.environ["COMMAND_KEYWORD"]
    gemini_api_key = os.environ["GEMINI_API_KEY"]
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true")

    def scrub(text):
        return text.replace(gemini_api_key, "<GEMINI_API_KEY>") if gemini_api_key else text

    config = check.load_json(check.CONFIG_PATH, None)
    state = check.load_json(check.STATE_PATH, {})
    allowlist = agent.load_allowlist()
    model = config.get("gemini_model", "gemini-3.5-flash")

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["MAIL_USERNAME"], os.environ["MAIL_APP_PASSWORD"])
    imap.select("INBOX")
    try:
        candidates = fetch_candidates(imap)
        print("found %d unseen candidate(s)" % len(candidates))
        for msg_id, msg in candidates:
            passed, gates = passes_gates(msg, command_sender, command_keyword)
            # Logging rule (§7.6): message-id and gate results/validated
            # action only -- never header values, never the body.
            print("candidate %s: from=%s subject=%s dkim=%s -> %s" % (
                msg_id.decode(), gates["from"], gates["subject"], gates["dkim"],
                "accepted" if passed else "rejected"))

            if passed:
                text = extract_body(msg)
                try:
                    verdict, changes, reply_text = agent.handle_command(
                        text, config, state, allowlist, model, gemini_api_key)
                except agent.GeminiError as err:
                    # Transient parse outage (503/quota/network after
                    # retries): leave the message UNSEEN so the next poll
                    # retries it, rather than consuming the command and
                    # replying "couldn't parse". Skip straight to the next
                    # candidate without marking this one seen.
                    print("candidate %s: parse temporarily unavailable (%s) -- "
                          "left unread to retry" % (msg_id.decode(), scrub(str(err))))
                    continue
                except Exception as err:
                    verdict, changes = "unknown", None
                    reply_text = "Couldn't process that command."
                    print("ERROR handling %s: %s" % (msg_id.decode(), scrub(str(err))))
                print("candidate %s: validated action=%s" % (msg_id.decode(), verdict))

                if dry_run:
                    print("DRY RUN: would apply %r and reply %r" % (changes, reply_text))
                else:
                    agent.apply_changes(changes, config, state)
                    subject = build_reply_subject(reply_text, command_keyword)
                    try:
                        mailer.send_email(subject, reply_text)
                    except Exception as err:
                        print("ERROR replying to %s: %s" % (msg_id.decode(), scrub(str(err))))

            # Marked \Seen whether or not it parsed -- processed once,
            # never reprocessed. Dry runs skip this too: nothing should
            # be consumed by a preview. (A transient parse outage above
            # `continue`s past this, so it stays unread for the next poll.)
            if not dry_run:
                imap.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        imap.logout()

    if not dry_run:
        check.save_json(check.CONFIG_PATH, config)
        check.save_json(check.STATE_PATH, state)


if __name__ == "__main__":
    main()
