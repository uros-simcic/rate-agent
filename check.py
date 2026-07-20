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

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# One call per watch per run. The key travels in the query string because
# that is the only auth mechanism currencyapi.net documents; the request URL
# is therefore secret-bearing and is NEVER printed (see _scrub in main()).
CURRENCYAPI_BASE = "https://currencyapi.net/api/v2/rates"


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
    which = crossed_bound(rate, watch)
    if which is None:
        return
    # Mark alerted and persist BEFORE the alert action: if alerting fails,
    # the flag is already saved and the alert will not repeat next run.
    entry["alerted"] = True
    save_state(state)
    # Stub for this step; Step 4 replaces it with the real readable email.
    print("ALERT %s: 1 %s = %s %s (%s bound crossed)" % (
        wid, from_code, rate, to_code, which))


def require_env(names):
    """Exit with one clear message naming every missing var, instead of
    letting a lazy os.environ[...] raise a bare KeyError deep in whichever
    code path happens to touch it first. Called once at startup so a
    misconfigured run fails immediately, not partway through."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit("missing required environment variable(s): %s" % ", ".join(missing))


def main():
    require_env(["CURRENCYAPI_KEY"])
    api_key = os.environ["CURRENCYAPI_KEY"]

    def scrub(text):
        """Redact the API key from any string before it is printed. Nothing
        today puts the key into error text (it lives only in the request
        URL, which is never logged), but this defends the invariant rather
        than trusting it silently."""
        return text.replace(api_key, "<CURRENCYAPI_KEY>") if api_key else text

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
