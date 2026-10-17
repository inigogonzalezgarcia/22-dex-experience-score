"""Telemetry as read from disk: devices.csv, events.jsonl, and the optional tickets.csv and sentiment.csv.

The formats are described in docs/telemetry-schema.md. Files are read with `utf-8-sig`, so a BOM written by
Excel or PowerShell 5.1 does no harm. Bad JSONL lines are skipped and counted, never fatal: a collector that
was killed half-way through a write should not stop the weekly report.
"""
from __future__ import annotations

import csv
import itertools
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

EVENT_TYPES = {
    "boot_seconds", "logon_seconds", "cpu_high_minutes", "mem_high_minutes",       # device performance
    "app_crash", "app_hang", "system_crash",                                         # stability
    "wifi_signal_pct", "wifi_disconnect", "network_latency_ms", "teams_call", "vpn_failure",  # network
}


def parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    v = str(value).strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    if "." in v:                                   # .NET writes 7 decimals; fromisoformat in 3.10 wants up to 6
        head, tail = v.split(".", 1)
        digits = "".join(itertools.takewhile(str.isdigit, tail))
        zone = tail[len(digits):]
        v = f"{head}.{digits[:6].ljust(6, '0')}{zone}"
    if len(v) == 10:                               # plain date
        v += "T00:00:00"
    dt = datetime.fromisoformat(v)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", ".").rstrip("%")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _yes(value) -> bool:
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "x")


@dataclass
class Device:
    name: str
    user: str = ""
    department: str = ""
    site: str = ""
    floor: str = ""
    model: str = ""
    os_build: str = ""
    ram_gb: float | None = None
    disk_free_pct: float | None = None
    battery_health_pct: float | None = None   # None for desktops (no battery)
    vip: bool = False

    @property
    def location(self) -> str:
        return f"{self.site} floor {self.floor}" if self.floor else self.site


@dataclass
class Event:
    ts: datetime
    device: str
    type: str
    value: object = None                      # number, app name, bugcheck code or "good"/"poor"

    @property
    def number(self) -> float | None:
        return _num(self.value)


@dataclass
class Ticket:
    opened_at: datetime
    device: str
    category: str = ""


@dataclass
class Answer:
    day: date
    device: str
    answer: float                             # 1 (bad) to 5 (great)


@dataclass
class Dataset:
    devices: dict[str, Device]
    events: list[Event]
    tickets: list[Ticket] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    bad_lines: int = 0                        # JSONL lines that could not be read
    unknown_types: int = 0                    # events of a type this version does not score
    unknown_devices: set[str] = field(default_factory=set)   # events for devices missing in devices.csv
    company: str = ""

    @property
    def now(self) -> datetime:
        return max((e.ts for e in self.events), default=datetime.now(timezone.utc))


def _rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def read_devices(path: Path) -> list[Device]:
    out = []
    for r in _rows(path):
        name = r.get("device") or r.get("devicename") or r.get("computername")
        if not name:
            continue
        out.append(Device(name=name.upper(), user=r.get("user", ""), department=r.get("department", ""),
                          site=r.get("site", ""), floor=r.get("floor", ""), model=r.get("model", ""),
                          os_build=r.get("os_build", ""), ram_gb=_num(r.get("ram_gb")),
                          disk_free_pct=_num(r.get("disk_free_pct")),
                          battery_health_pct=_num(r.get("battery_health_pct")), vip=_yes(r.get("vip"))))
    return out


def read_events(path: Path) -> tuple[list[Event], int, int]:
    """Returns (events, bad_lines, unknown_types). Blank lines are ignored; anything unreadable is counted."""
    events, bad, unknown = [], 0, 0
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts = parse_ts(d["ts"])
                typ = str(d["type"]).strip().lower()
                dev = str(d["device"]).strip().upper()
                if ts is None or not dev:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                bad += 1
                continue
            if typ not in EVENT_TYPES:
                unknown += 1
                continue
            events.append(Event(ts, dev, typ, d.get("value")))
    return events, bad, unknown


def read_tickets(path: Path) -> list[Ticket]:
    out = []
    for r in _rows(path):
        ts = parse_ts(r.get("opened_at"))
        if ts and r.get("device"):
            out.append(Ticket(ts, r["device"].upper(), r.get("category", "")))
    return out


def read_sentiment(path: Path, devices: dict[str, Device]) -> list[Answer]:
    """Answers keyed by device; a row with only a user is mapped to that user's device(s)."""
    by_user: dict[str, list[str]] = {}
    for d in devices.values():
        if d.user:
            by_user.setdefault(d.user.lower(), []).append(d.name)
    out = []
    for r in _rows(path):
        ts, ans = parse_ts(r.get("date")), _num(r.get("answer"))
        if ts is None or ans is None or not 1 <= ans <= 5:
            continue
        targets = [r["device"].upper()] if r.get("device") else by_user.get(r.get("user", "").lower(), [])
        out += [Answer(ts.date(), t, ans) for t in targets]
    return out


def sniff(path: Path) -> str:
    """Which input a file is, from its extension and CSV header: devices, events, tickets or sentiment."""
    if path.suffix.lower() in (".jsonl", ".ndjson", ".json"):
        return "events"
    with open(path, encoding="utf-8-sig") as fh:
        header = {h.strip().lower() for h in next(csv.reader(fh), [])}   # Export-Csv quotes every name
    if "opened_at" in header:
        return "tickets"
    if "answer" in header:
        return "sentiment"
    if "device" in header or "devicename" in header:
        return "devices"
    raise ValueError(f"{path}: not a devices, tickets or sentiment CSV (see docs/telemetry-schema.md)")


def load(paths: list[Path], company: str = "") -> Dataset:
    kinds: dict[str, list[Path]] = {"devices": [], "events": [], "tickets": [], "sentiment": []}
    for p in paths:
        kinds[sniff(p)].append(p)
    if not kinds["devices"] or not kinds["events"]:
        raise ValueError("Need at least one devices CSV and one events JSONL file.")
    devices: dict[str, Device] = {}
    for p in kinds["devices"]:
        for d in read_devices(p):
            devices[d.name] = d                 # a later file wins: newer collector run
    ds = Dataset(devices, [], company=company)
    for p in kinds["events"]:
        evs, bad, unknown = read_events(p)
        ds.bad_lines += bad
        ds.unknown_types += unknown
        for e in evs:
            if e.device in devices:
                ds.events.append(e)
            else:
                ds.unknown_devices.add(e.device)
    ds.events.sort(key=lambda e: e.ts)
    for p in kinds["tickets"]:
        ds.tickets += read_tickets(p)
    for p in kinds["sentiment"]:
        ds.answers += read_sentiment(p, devices)
    return ds
