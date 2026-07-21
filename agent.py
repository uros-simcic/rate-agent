#!/usr/bin/env python3
"""Command core for the agent's email/issue channels: validates a parsed
command against config/state/allowlist and describes what should change.

This is the heart of the "LLM proposes, code disposes" design (see
README): Gemini (wired in a later step) only ever returns a JSON object
against a fixed schema, display-only text and enum/number fields. Every
field coming out of that JSON is validated here against allowlists and
bounds before it can touch config, state, or a reply -- the model's
output never becomes a git operation, a shell command, or a URL, and
`validate_command` never trusts a field it hasn't checked.
"""

import math
import re

MAX_WATCHES = 10
CODE_RE = re.compile(r"^[A-Z]{3,5}$")
ACTIONS = {"add", "remove", "pause", "resume", "reset", "list", "unknown"}


def _valid_bound(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def validate_command(parsed, config, state, allowlist):
    """Pure function: parsed command dict (matching the §5 Gemini schema) +
    current config + current state + the currency allowlist -> (verdict,
    changes, reply_text). Never mutates config/state -- `changes` describes
    what a later apply step should do, or None if nothing changes.
    Unit-test friendly by design: no I/O, no Gemini call, just dicts in,
    dicts out.
    """
    action = parsed.get("action")
    if action not in ACTIONS or action == "unknown":
        reason = parsed.get("reason") or "not a recognized command"
        return "unknown", None, "Couldn't parse that: %s" % reason

    if action == "list":
        return "listed", None, _render_list(config, state)

    from_code = (parsed.get("from") or "").upper()
    to_code = (parsed.get("to") or "").upper()
    for code in (from_code, to_code):
        if not CODE_RE.match(code):
            return "rejected", None, "Invalid currency code: %r" % code
        if code not in allowlist:
            return "rejected", None, "Unknown currency: %s" % code

    # Derived, never model-supplied -- the same id no matter how the model
    # capitalized or phrased the pair.
    wid = "%s_%s" % (from_code.lower(), to_code.lower())
    watches = config.get("watches", [])
    existing = next((w for w in watches if w["id"] == wid), None)

    if action == "add":
        return _validate_add(parsed, from_code, to_code, wid, watches, existing)

    if existing is None:
        return "rejected", None, "No watch for %s to %s." % (from_code, to_code)
    if action == "remove":
        return "removed", {"op": "remove_watch", "id": wid}, \
            "Stopped watching %s to %s." % (from_code, to_code)
    if action == "pause":
        return "paused", {"op": "set_paused", "id": wid, "paused": True}, \
            "Paused the %s to %s watch." % (from_code, to_code)
    if action == "resume":
        return "resumed", {"op": "set_paused", "id": wid, "paused": False}, \
            "Resumed the %s to %s watch." % (from_code, to_code)
    if action == "reset":
        return "reset", {"op": "reset_alert", "id": wid}, \
            "Reset the %s to %s alert." % (from_code, to_code)
    return "unknown", None, "Couldn't parse that: unsupported action."


def _validate_add(parsed, from_code, to_code, wid, watches, existing):
    above = parsed.get("above")
    below = parsed.get("below")
    if above is None and below is None:
        return "rejected", None, "Need an above or below threshold to add a watch."
    for label, value in (("above", above), ("below", below)):
        if value is not None and not _valid_bound(value):
            return "rejected", None, "%s must be a positive number." % label

    watch = {"id": wid, "from": from_code, "to": to_code}
    if above is not None:
        watch["above"] = float(above)
    if below is not None:
        watch["below"] = float(below)

    if existing is not None:
        if existing.get("above") == watch.get("above") and existing.get("below") == watch.get("below"):
            return "already_watching", None, "Already watching %s to %s." % (from_code, to_code)
        return "updated", {"op": "upsert_watch", "watch": watch}, \
            "Updated the %s to %s watch." % (from_code, to_code)

    if len(watches) >= MAX_WATCHES:
        return "rejected", None, "Watch limit reached (%d max)." % MAX_WATCHES
    return "added", {"op": "upsert_watch", "watch": watch}, \
        "Now watching %s to %s." % (from_code, to_code)


def _render_list(config, state):
    watches = config.get("watches", [])
    if not watches:
        return "No watches configured."
    lines = []
    for w in watches:
        entry = state.get(w["id"], {})
        bounds = []
        if w.get("above") is not None:
            bounds.append("above %s" % w["above"])
        if w.get("below") is not None:
            bounds.append("below %s" % w["below"])
        flags = []
        if entry.get("paused"):
            flags.append("paused")
        if entry.get("alerted"):
            flags.append("alerted")
        lines.append("%s to %s (%s)%s" % (
            w["from"], w["to"], " / ".join(bounds),
            " [%s]" % ", ".join(flags) if flags else ""))
    return "Watching:\n" + "\n".join(lines)
