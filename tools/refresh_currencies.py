#!/usr/bin/env python3
"""Regenerate the currency allowlist from currencyapi.net's own rates
response -- the keys of any `rates` object are the complete set of
currency codes the account can actually query (the "allowlist trick").
Run manually from your instance repo directory, with this engine checked
out alongside (or on PYTHONPATH); writes currencies.json to the current
directory for you to review and commit."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import check  # noqa: E402 -- path must be set up first


def main():
    check.require_env(["CURRENCYAPI_KEY"])
    api_key = os.environ["CURRENCYAPI_KEY"]
    rates = check.fetch_all_rates(api_key)
    codes = sorted(rates.keys())
    with open("currencies.json", "w", encoding="utf-8") as f:
        json.dump(codes, f, indent=2)
    print("wrote %d currency codes to currencies.json" % len(codes))


if __name__ == "__main__":
    main()
