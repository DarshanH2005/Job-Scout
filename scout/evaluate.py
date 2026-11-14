"""Measure ranking quality against the decisions recorded in a pilot sheet.

The pilot shortlist is a labelled set: every reviewed row carries a decision such
as ``shortlist``, ``reject:level`` or ``unclear:eligibility``. This module turns
those labels into the numbers the roadmap asks for — precision at the top of the
list, hard exclusions the ranking failed to filter, and how the rule buckets
performed — without touching the database or the labels themselves.

Nothing here writes: it reads CSV files and prints a report.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

HARD = ("reject:level", "reject:location", "reject:closed")
SOFT = ("reject:role", "reject:other")
DECISIONS = ("shortlist", "unclear:eligibility") + HARD + SOFT
REJECTIONS = HARD + SOFT


def _priority(value):
    head = str(value or "").strip().split(" ", 1)[0]
    return int(head) if head.isdigit() else None


def _decision(row):
    return (row.get("decision") or "").strip().casefold().replace(" ", "")


def load(paths):
    """Read every pilot sheet given as a file or directory, in filename order."""
    files, rows = [], []
    for entry in paths:
        entry = Path(entry)
        if entry.is_dir():
            found = sorted(entry.glob("*shortlist*.csv"))
            if not found:
                raise FileNotFoundError(f"No pilot sheet found in {entry}")
            files.extend(found)
        elif entry.is_file():
            files.append(entry)
        else:
            raise FileNotFoundError(f"Labels not found: {entry}")
    for file in files:
        with file.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["_sheet"] = file.name
                rows.append(row)
    return files, rows


def summarise(rows, top=10):
    labelled = [row for row in rows if _decision(row) in DECISIONS]
    unknown = sorted({_decision(row) for row in rows
                      if _decision(row) and _decision(row) not in DECISIONS})
    rejected = [row for row in labelled if _decision(row) in REJECTIONS]
    reasons = {}
    for row in labelled:
        reasons[_decision(row)] = reasons.get(_decision(row), 0) + 1
    ordered = sorted((row for row in labelled if _priority(row.get("review_priority")) is not None),
                     key=lambda row: _priority(row.get("review_priority")))
    top_rows = [row for row in ordered if _priority(row.get("review_priority")) <= top]
    shortlisted_top = sum(1 for row in top_rows if _decision(row) == "shortlist")
    hard_top = sum(1 for row in top_rows if _decision(row) in HARD)
    buckets = {}
    for row in labelled:
        bucket = str(row.get("score") or "?").strip() or "?"
        stats = buckets.setdefault(bucket, {"labelled": 0, "shortlist": 0})
        stats["labelled"] += 1
        if _decision(row) == "shortlist":
            stats["shortlist"] += 1
    return {
        "sheets": sorted({row["_sheet"] for row in rows}),
        "rows": len(rows),
        "labelled": len(labelled),
        "unlabelled": len(rows) - len(labelled),
        "top": top,
        "reviewed": len(top_rows),
        "precision_at_top": round(shortlisted_top / len(top_rows), 3) if top_rows else None,
        "shortlisted_in_top": shortlisted_top,
        "hard_exclusions_in_top": hard_top,
        "by_decision": dict(sorted(reasons.items())),
        "by_bucket": {bucket: {**stats, "rate": round(stats["shortlist"] / stats["labelled"], 3)}
                      for bucket, stats in sorted(buckets.items())},
        "unknown_decisions": unknown,
        "rejections_missing_reason": sum(1 for row in rejected if not (row.get("reject_reason") or "").strip()),
        "shortlisted": reasons.get("shortlist", 0),
    }


def render(report, target=0.8):
    lines = ["Job Scout ranking evaluation"]
    if report["sheets"]:
        lines.append("  sheets            " + ", ".join(report["sheets"]))
    lines.append(f"  rows read         {report['rows']:>4}")
    lines.append(f"  labelled          {report['labelled']:>4}   ({report['unlabelled']} still blank)")
    if not report["labelled"]:
        lines += ["", "  No decisions recorded yet. Open the pilot sheet, fill the decision column,",
                  "  then run this report again. A decision per reviewed row is the whole dataset."]
        return "\n".join(lines)
    lines.append("  decisions         " + ", ".join(f"{name} {count}" for name, count in report["by_decision"].items()))
    if report["precision_at_top"] is None:
        lines.append(f"  precision@{report['top']:<3}      n/a   no rows with a review priority")
    else:
        verdict = "meets" if report["precision_at_top"] >= target else "below"
        lines.append(f"  precision@{report['top']:<3}      {report['shortlisted_in_top']}/{report['reviewed']}"
                     f" = {report['precision_at_top']:.0%}   target {target:.0%}, {verdict}")
    lines.append(f"  hard exclusions   {report['hard_exclusions_in_top']} in the top {report['top']}"
                 "   (level, location or closed)")
    lines.append("")
    lines.append("  by rule bucket")
    for bucket, stats in report["by_bucket"].items():
        lines.append(f"    score {bucket:<4} labelled {stats['labelled']:>3}   shortlisted {stats['shortlist']:>2}"
                     f"   useful {stats['rate']:.0%}")
    problems = []
    if report["unknown_decisions"]:
        problems.append("unrecognised decisions: " + ", ".join(report["unknown_decisions"]))
    if report["rejections_missing_reason"]:
        problems.append(f"{report['rejections_missing_reason']} rejection(s) without a reason")
    if problems:
        lines += ["", "  data quality"] + [f"    {problem}" for problem in problems]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scout.evaluate",
                                     description="Measure ranking quality against pilot decisions (read-only).")
    parser.add_argument("--labels", type=Path, nargs="+", required=True,
                        help="Pilot sheets or a directory containing them")
    parser.add_argument("--top", type=int, default=10, help="How many top rows count as the shortlist window")
    parser.add_argument("--target", type=float, default=0.8, help="Useful-precision target for the top window")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    try:
        _, rows = load(args.labels)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    report = summarise(rows, top=args.top)
    print(json.dumps(report, indent=2) if args.json else render(report, target=args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
