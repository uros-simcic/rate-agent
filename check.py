#!/usr/bin/env python3
"""Scheduled exchange-rate check: for each watched pair, fetch the current
rate, compare it against the watch's threshold(s), and alert once when a
threshold is crossed. Rate direction matches typing "AED to EUR" into a
search box — the rate is how much 1 unit of `from` is worth in `to`. No
inversion anywhere, ever.

This is the "code disposes" half of the project (see README §"LLM proposes,
code disposes"): everything here is deterministic. The LLM command channel
lives in agent.py and never runs a check or sends an alert.
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# One call per watch per run. The key travels in the query string because
# that is the only auth mechanism currencyapi.net documents; the request URL
# is therefore secret-bearing and is NEVER printed (see _scrub in main()).
CURRENCYAPI_BASE = "https://currencyapi.net/api/v2/rates"

HISTORY_CAP = 90
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def utc_timestamp():
    """Current time as an ISO-8601 UTC string with a trailing 'Z', e.g.
    "2026-07-20T06:00:12Z" — the exact format history entries use, so the
    alert email's timestamp and the history it summarizes never drift into
    two different UTC string styles."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    """Read a JSON file, or return `default` if it does not exist yet. State
    legitimately starts absent on a brand-new instance, so callers pass {};
    config must exist, so main() passes None and a missing file raises."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is None:
            raise
        return default


def save_state(state):
    """Persist state to disk. Called immediately after a watch is marked
    alerted so that a crash later in the run cannot lose that flag and
    re-send the same alert on the next run — the duplicate-alert bug this
    write-ordering exists to prevent."""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_rate(from_code, to_code, api_key):
    """Return the current rate (float) of 1 `from` in `to` from
    currencyapi.net. from/to come from validated config, never from model
    output; they are URL-quoted defensively all the same."""
    url = "%s?key=%s&base=%s&output=JSON" % (
        CURRENCYAPI_BASE,
        urllib.parse.quote(api_key),
        urllib.parse.quote(from_code),
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    rates = data.get("rates")
    if not isinstance(rates, dict) or to_code not in rates:
        raise ValueError("no rate for %s->%s in API response" % (from_code, to_code))
    return float(rates[to_code])


def crossed_bound(rate, watch):
    """Return which bound the rate crossed ("above"/"below"), or None. A
    watch may set `above`, `below`, or both. `above` fires when the rate
    rises over the ceiling; `below` when it falls under the floor."""
    above = watch.get("above")
    if above is not None and rate > above:
        return "above"
    below = watch.get("below")
    if below is not None and rate < below:
        return "below"
    return None


def format_rate(rate):
    """Comma-grouped, readable rate: "96,000" for large/whole values,
    "0.245" for small ones — never Python's raw float repr."""
    if abs(rate) >= 1:
        text = "{:,.2f}".format(rate)
        return text[:-3] if text.endswith(".00") else text
    return "{:.6g}".format(rate)


def sparkline(history):
    """Min-max normalize history rates onto SPARK_CHARS; an all-equal
    history renders the middle char for every point. `history` is a list
    of [timestamp, rate] pairs; pure function, stdlib only."""
    rates = [r for _, r in history]
    if not rates:
        return ""
    lo, hi = min(rates), max(rates)
    if lo == hi:
        bar = SPARK_CHARS[len(SPARK_CHARS) // 2] * len(rates)
    else:
        span = hi - lo
        bar = "".join(
            SPARK_CHARS[min(int((r - lo) / span * len(SPARK_CHARS)), len(SPARK_CHARS) - 1)]
            for r in rates
        )
    return "%s (last %d checks)" % (bar, len(rates))


def send_alert_email(wid, from_code, to_code, rate, which, bound_value, history):
    """Send the alert as a plain-text email. Raises smtplib.SMTPException
    on failure; callers already wrap check_watch in try/except, so a send
    failure is logged (scrubbed) and skipped like any other watch error —
    state.alerted was already saved beforehand, so it will not repeat."""
    mail_user = os.environ["MAIL_USERNAME"]
    mail_pass = os.environ["MAIL_APP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]

    subject = "Rate alert: 1 %s = %s %s" % (from_code, format_rate(rate), to_code)
    body = (
        "Watch: %s\n"
        "1 %s = %s %s\n"
        "%s bound crossed (%s)\n"
        "Checked: %s\n"
        "%s\n"
    ) % (wid, from_code, format_rate(rate), to_code, which, bound_value,
         utc_timestamp(), sparkline(history))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_user
    msg["To"] = mail_to

    recipients = [addr.strip() for addr in mail_to.split(",") if addr.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(mail_user, mail_pass)
        server.sendmail(mail_user, recipients, msg.as_string())


def check_watch(watch, state, api_key):
    """Check a single watch. Callers wrap this in try/except so one failing
    fetch cannot abort the run for the others."""
    wid = watch["id"]
    from_code = watch["from"].upper()
    to_code = watch["to"].upper()
    entry = state.setdefault(wid, {})
    if entry.get("paused"):
        print("skipping %s (paused)" % wid)
        return
    if entry.get("alerted"):
        print("skipping %s (already alerted)" % wid)
        return
    rate = fetch_rate(from_code, to_code, api_key)
    # Recorded for every successful check, alert or not — the sparkline
    # needs a real trend line for watches that never cross a bound.
    history = entry.setdefault("history", [])
    history.append([utc_timestamp(), rate])
    del history[:-HISTORY_CAP]

    which = crossed_bound(rate, watch)
    if which is None:
        return
    # Mark alerted and persist BEFORE the alert action: if alerting fails,
    # the flag is already saved and the alert will not repeat next run.
    entry["alerted"] = True
    save_state(state)
    send_alert_email(wid, from_code, to_code, rate, which, watch.get(which), history)


def require_env(names):
    """Exit with one clear message naming every missing var, instead of
    letting a lazy os.environ[...] raise a bare KeyError deep in whichever
    code path happens to touch it first. Called once at startup so a
    misconfigured run fails immediately, not partway through."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit("missing required environment variable(s): %s" % ", ".join(missing))


def main():
    require_env(["CURRENCYAPI_KEY", "MAIL_USERNAME", "MAIL_APP_PASSWORD", "MAIL_TO"])
    api_key = os.environ["CURRENCYAPI_KEY"]
    mail_pass = os.environ["MAIL_APP_PASSWORD"]

    def scrub(text):
        """Redact secrets from any string before it is printed. The API key
        lives only in the request URL and the mail password only in the
        SMTP login call — neither should reach an exception message today,
        but this defends the invariant rather than trusting it silently."""
        if api_key:
            text = text.replace(api_key, "<CURRENCYAPI_KEY>")
        if mail_pass:
            text = text.replace(mail_pass, "<MAIL_APP_PASSWORD>")
        return text

    config = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    for watch in config.get("watches", []):
        try:
            check_watch(watch, state, api_key)
        except Exception as err:
            # One watch's failure is logged (key-scrubbed) and skipped; the
            # remaining watches still run.
            print("ERROR checking %s: %s" % (watch.get("id", "?"), scrub(str(err))),
                  file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()
