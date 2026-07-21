#!/usr/bin/env python3
"""Email command channel: polls the agent's mailbox for candidate command
messages and gates them by sender, subject keyword, and DKIM. This step
only fetches and gates -- print-only, no \\Seen, no pipeline wiring (that
lands in the next step once gating itself is verified against a real
inbox).

Command text is untrusted input (see agent.py's parse_command) -- these
three gates decide whether a message is even a CANDIDATE for that
pipeline. They do not themselves authorize any config/state change; only
validate_command()'s allowlist/bounds checks decide that.
"""

import email
import imaplib
import os


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
    """Search UNSEEN, return [(msg_id_bytes, email.message.Message), ...]."""
    status, data = imap.search(None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    candidates = []
    for msg_id in data[0].split():
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        candidates.append((msg_id, email.message_from_bytes(msg_data[0][1])))
    return candidates


def main():
    for name in ("MAIL_USERNAME", "MAIL_APP_PASSWORD", "COMMAND_SENDER", "COMMAND_KEYWORD"):
        if not os.environ.get(name):
            raise SystemExit("missing required environment variable: %s" % name)

    command_sender = os.environ["COMMAND_SENDER"]
    command_keyword = os.environ["COMMAND_KEYWORD"]

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["MAIL_USERNAME"], os.environ["MAIL_APP_PASSWORD"])
    imap.select("INBOX")
    try:
        candidates = fetch_candidates(imap)
        print("found %d unseen candidate(s)" % len(candidates))
        for msg_id, msg in candidates:
            passed, gates = passes_gates(msg, command_sender, command_keyword)
            # Logging rule (§7.6): message-id and gate results only --
            # never the header values themselves, never the body.
            print("candidate %s: from=%s subject=%s dkim=%s -> %s" % (
                msg_id.decode(), gates["from"], gates["subject"], gates["dkim"],
                "would accept" if passed else "would reject"))
    finally:
        imap.logout()


if __name__ == "__main__":
    main()
