"""Command line: `demo` builds everything from a fictional fleet, `report` runs on your own telemetry files."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from . import insights, report, simulate
from .model import load, parse_ts
from .score import Scores, load_config


def run(files: list[Path], out: Path, title: str, cfg: dict, now: datetime | None = None) -> dict:
    ds = load(files)
    sc = Scores(ds, cfg, now)
    fixes = insights.top_fixes(sc)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text(report.render_html(sc, fixes, title), encoding="utf-8")
    report.write_device_scores(sc, out / "device_scores.csv")
    report.write_top_fixes(fixes, out / "top_fixes.csv")
    (out / "review.md").write_text(report.review_md(sc, fixes, title), encoding="utf-8")
    sm = report.summary(sc, fixes)
    sm["fixes"] = fixes
    return sm


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexscore", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="generate a fictional fleet and build the outputs")
    d.add_argument("--out", type=Path, default=Path("output"))
    d.add_argument("--data", type=Path, default=Path("sample_data"))
    r = sub.add_parser("report", help="build the outputs from your own telemetry")
    r.add_argument("files", type=Path, nargs="+",
                   help="devices CSV(s) and events JSONL file(s), optionally tickets.csv and sentiment.csv; "
                        "the kind of each file is read from its header")
    r.add_argument("--out", type=Path, default=Path("output"))
    r.add_argument("--title", default="Digital experience score")
    r.add_argument("--config", type=Path, help="TOML file overriding thresholds and weights (Python 3.11+)")
    r.add_argument("--now", help="score the weeks that end at midnight before this date (YYYY-MM-DD); "
                                 "default: the time of the last event")
    a = p.parse_args(argv)

    if a.cmd == "demo":
        sm = run(simulate.write_demo(a.data), a.out, "Contoso · digital experience score (demo data)", load_config(None),
                 simulate.DEMO_NOW)
    else:
        sm = run(a.files, a.out, a.title, load_config(a.config), parse_ts(a.now) if a.now else None)
    bad = f" ({sm['bad_lines']} bad line{'s' if sm['bad_lines'] != 1 else ''} skipped)" if sm["bad_lines"] else ""
    change = "n/a" if sm["change"] is None else f"{sm['change']:+.2f}"
    fleet = "n/a" if sm["fleet"] is None else f"{sm['fleet']:.1f}"
    print(f"{sm['scored']} of {sm['devices']} devices scored · fleet score {fleet} ({change} vs last week) · "
          f"{sm['below6']} below 6 · {sm['events']:,} events{bad} · top fix: {sm['top'] or 'none'}. Outputs in {a.out}/")
    return 0
