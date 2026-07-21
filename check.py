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
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import mailer

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# One call per RUN (not per watch): the free plan has a fixed base
# currency and rejects any base= param with a 403 ("Your subscription
# plan does not allow you to select a base currency") -- confirmed live
# against this project's own key. Every rate below is computed as a cross
# rate through that fixed base instead. The key travels in the query
# string because that is the only auth mechanism currencyapi.net
# documents; the request URL is therefore secret-bearing and is NEVER
# printed (see _scrub in main()).
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


def fetch_all_rates(api_key):
    """Fetch every currencyapi.net rate against its (free-plan-fixed) base
    currency in one call. Raises ValueError with the response body on an
    HTTP error -- a bare "HTTP Error 403: Forbidden" gave no clue why the
    free plan rejected a base= param until the body was captured; mirrors
    gemini_client.py's HTTPError handling."""
    url = "%s?key=%s&output=JSON" % (CURRENCYAPI_BASE, urllib.parse.quote(api_key))
    req = urllib.request.Request(url, headers={"User-Agent": "rate-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read(300).decode("utf-8", "replace")
        except OSError:
            pass
        raise ValueError("HTTP %d fetching rates: %s" % (err.code, detail)) from err
    rates = data.get("rates")
    if not isinstance(rates, dict):
        raise ValueError("no rates in API response")
    return rates


def cross_rate(rates, from_code, to_code):
    """1 `from` in `to`, computed through the fixed base both are quoted
    against: rates[X] is how many X per 1 base unit, so 1 from = 1/rates[from]
    base units = rates[to]/rates[from] `to` units. Same "AED to EUR" search-box
    direction as everywhere else -- no inversion."""
    if from_code not in rates or to_code not in rates:
        missing = [c for c in (from_code, to_code) if c not in rates]
        raise ValueError("no rate for %s in API response" % ", ".join(missing))
    return float(rates[to_code]) / float(rates[from_code])


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
    """Build and send the alert. Raises smtplib.SMTPException on failure;
    callers already wrap check_watch in try/except, so a send failure is
    logged (scrubbed) and skipped like any other watch error --
    state.alerted was already saved beforehand, so it will not repeat."""
    subject = "Rate alert: 1 %s = %s %s" % (from_code, format_rate(rate), to_code)
    body = (
        "Watch: %s\n"
        "1 %s = %s %s\n"
        "%s bound crossed (%s)\n"
        "Checked: %s\n"
        "%s\n"
    ) % (wid, from_code, format_rate(rate), to_code, which, bound_value,
         utc_timestamp(), sparkline(history))
    mailer.send_email(subject, body)


def check_watch(watch, state, rates, dry_run=False):
    """Check a single watch against one run's already-fetched rates dict.
    Callers wrap this in try/except so one watch with a missing currency
    code cannot abort the run for the others. In dry_run, every write to
    state.json is skipped and the alert becomes a print preview, so a
    dispatched dry run can never mark a watch alerted or send mail."""
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
    rate = cross_rate(rates, from_code, to_code)
    # Recorded for every successful check, alert or not — the sparkline
    # needs a real trend line for watches that never cross a bound. Kept
    # in-memory even in dry_run (harmless — never written to disk), so the
    # preview sparkline below looks like the real one would.
    history = entry.setdefault("history", [])
    history.append([utc_timestamp(), rate])
    del history[:-HISTORY_CAP]

    which = crossed_bound(rate, watch)
    if which is None:
        return
    if dry_run:
        print("DRY RUN: would alert %s: 1 %s = %s %s (%s bound crossed)" % (
            wid, from_code, format_rate(rate), to_code, which))
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

    # Cron runs pass no env at all, so DRY_RUN is empty and this is False —
    # the real run. Only an explicit "1"/"true" from workflow_dispatch
    # turns on the preview path.
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true")

    config = load_json(CONFIG_PATH, None)
    state = load_json(STATE_PATH, {})
    try:
        rates = fetch_all_rates(api_key)
    except Exception as err:
        # No rates means nothing in this run can be checked; log (scrubbed)
        # and exit cleanly rather than crash -- an API outage is transient
        # and the next scheduled run will simply try again.
        print("ERROR fetching rates: %s" % scrub(str(err)), file=sys.stderr)
        return

    for watch in config.get("watches", []):
        try:
            check_watch(watch, state, rates, dry_run)
        except Exception as err:
            # One watch's failure is logged (key-scrubbed) and skipped; the
            # remaining watches still run.
            print("ERROR checking %s: %s" % (watch.get("id", "?"), scrub(str(err))),
                  file=sys.stderr)
    if not dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
