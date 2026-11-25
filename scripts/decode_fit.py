"""Decode a FIT file and report which per-second fields are present.

Usage: uv run python -I scripts/decode_fit.py <path/to/file.fit> [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from garmin_fit_sdk import Decoder, Stream

# What a running analytics product needs per sample, mapped to FIT record fields.
CHECKLIST = {
    "Heart rate": ["heart_rate"],
    "Pace / speed": ["enhanced_speed", "speed"],
    "Distance": ["distance"],
    "Cadence": ["cadence", "fractional_cadence"],
    "Power": ["power", "accumulated_power"],
    "GPS": ["position_lat", "position_long"],
    "Altitude": ["enhanced_altitude", "altitude"],
    "Grade": ["grade"],
    "Temperature": ["temperature"],
    "Vertical oscillation": ["vertical_oscillation"],
    "Ground contact time": ["stance_time"],
    "GCT balance": ["stance_time_balance"],
    "Stride length": ["step_length"],
    "Vertical ratio": ["vertical_ratio"],
    "Respiration rate": ["respiration_rate", "enhanced_respiration_rate"],
    "Core temp / other sensors": ["core_temperature"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fit", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    stream = Stream.from_file(str(args.fit))
    decoder = Decoder(stream)
    print(f"is_fit={decoder.is_fit()} integrity={decoder.check_integrity()}")
    decoder = Decoder(Stream.from_file(str(args.fit)))  # integrity check consumes the stream
    messages, errors = decoder.read(
        convert_types_to_strings=True,
        convert_datetimes_to_dates=True,
        expand_sub_fields=True,
        expand_components=True,
        merge_heart_rates=True,  # pulls HR from hr messages (swim/strap buffering) into records
    )
    if errors:
        print("decode errors:", errors)

    print("\n== message types")
    for k, v in sorted(messages.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:<34} {len(v)}")

    records = messages.get("record_mesgs", [])
    n = len(records)
    times = [r["timestamp"] for r in records if isinstance(r.get("timestamp"), datetime)]
    deltas = Counter(int((b - a).total_seconds()) for a, b in zip(times, times[1:]))
    print(f"\n== record messages: {n}, span {times[0]} -> {times[-1]}")
    print(f"  sample interval (s) histogram: {deltas.most_common(6)}")

    field_counts: Counter[str] = Counter()
    dev_fields: Counter[str] = Counter()
    for r in records:
        for k, v in r.items():
            if k == "developer_fields":
                for dk in v:
                    dev_fields[str(dk)] += 1
            elif v is not None:
                field_counts[str(k)] += 1

    print("\n== record fields (non-null count / records)")
    for k, c in sorted(field_counts.items(), key=lambda kv: -kv[1]):
        sample = next((r[k] for r in records if r.get(k) is not None), None)
        print(f"  {k:<32} {c:>6}/{n}  {c / n:6.1%}  e.g. {sample!r}"[:120])
    if dev_fields:
        print("\n== developer fields on records:", dict(dev_fields))
        for d in messages.get("field_description_mesgs", []):
            print("   ", {k: d.get(k) for k in ("developer_data_index", "field_definition_number", "field_name", "units")})

    print("\n== checklist")
    report = {}
    for label, keys in CHECKLIST.items():
        hits = {k: field_counts[k] for k in keys if field_counts.get(k)}
        status = "PRESENT" if hits else "MISSING"
        report[label] = {"status": status, "fields": hits, "coverage": max(hits.values()) / n if hits else 0}
        print(f"  {label:<26} {status:<8} {hits}")

    for name in ("session_mesgs", "lap_mesgs", "device_info_mesgs", "user_profile_mesgs", "zones_target_mesgs",
                 "sport_mesgs", "time_in_zone_mesgs", "split_mesgs", "split_summary_mesgs", "hrv_mesgs"):
        msgs = messages.get(name, [])
        if not msgs:
            continue
        keys = sorted({str(k) for m in msgs for k, v in m.items() if v is not None})
        print(f"\n== {name} ({len(msgs)}): non-null fields")
        print("  " + ", ".join(keys))

    if args.json:
        args.json.write_text(json.dumps({
            "records": n,
            "interval_histogram": deltas.most_common(10),
            "record_fields": dict(field_counts),
            "developer_fields": dict(dev_fields),
            "checklist": report,
            "message_counts": {k: len(v) for k, v in messages.items()},
            "session": messages.get("session_mesgs", [{}])[0],
            "devices": messages.get("device_info_mesgs", []),
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
