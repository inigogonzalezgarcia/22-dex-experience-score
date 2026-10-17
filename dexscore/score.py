"""The scoring model: metric -> 0-10, metrics -> category, categories -> device, devices -> site, department, fleet.

Every number used here is in DEFAULT_CONFIG and explained in docs/scoring.md. A TOML file can override any of
them (Python 3.11+, which ships `tomllib`; on 3.10 the defaults are used).
"""
from __future__ import annotations

import copy
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .model import Answer, Dataset, Device, Event

CATEGORIES = ["performance", "stability", "network", "hardware", "sentiment"]

DEFAULT_CONFIG: dict = {
    "window_days": 7,                # one score per device per 7-day window
    "weeks": 6,                      # how many windows the trend shows
    "categories": {
        "performance": {"label": "Device performance", "weight": 0.30},
        "stability": {"label": "Stability", "weight": 0.30},
        "network": {"label": "Network & collaboration", "weight": 0.25},
        "hardware": {"label": "Hardware health", "weight": 0.15},
        "sentiment": {"label": "Sentiment", "weight": 0.10},
    },
    # agg: median / mean of the values in the window; per_day = events per active day; minutes_per_day = sum of
    # values per active day; poor_ratio = share of "poor" values; device = a column of devices.csv;
    # sentiment = mean survey answer. A metric scores 10 at `good`, 0 at `poor`, linear in between.
    "metrics": {
        "boot_seconds":       {"category": "performance", "agg": "median", "good": 60, "poor": 180, "label": "Boot time", "unit": "s"},
        "logon_seconds":      {"category": "performance", "agg": "median", "good": 20, "poor": 90, "label": "Logon time", "unit": "s"},
        "cpu_high_minutes":   {"category": "performance", "agg": "minutes_per_day", "good": 15, "poor": 120, "label": "High CPU", "unit": "min/day"},
        "mem_high_minutes":   {"category": "performance", "agg": "minutes_per_day", "good": 15, "poor": 120, "label": "Memory pressure", "unit": "min/day"},
        "disk_free_pct":      {"category": "performance", "agg": "device", "good": 25, "poor": 5, "label": "Free disk", "unit": "%"},
        "app_crash":          {"category": "stability", "agg": "per_day", "good": 0, "poor": 1.0, "label": "App crashes", "unit": "/day"},
        "app_hang":           {"category": "stability", "agg": "per_day", "good": 0, "poor": 1.5, "label": "App hangs", "unit": "/day"},
        "system_crash":       {"category": "stability", "agg": "per_day", "good": 0, "poor": 0.4, "weight": 2, "label": "System crashes", "unit": "/day"},
        "wifi_signal_pct":    {"category": "network", "agg": "mean", "good": 70, "poor": 40, "label": "Wi-Fi signal", "unit": "%"},
        "wifi_disconnect":    {"category": "network", "agg": "per_day", "good": 0, "poor": 3, "label": "Wi-Fi drops", "unit": "/day"},
        "network_latency_ms": {"category": "network", "agg": "median", "good": 60, "poor": 250, "label": "Latency", "unit": "ms"},
        "teams_call":         {"category": "network", "agg": "poor_ratio", "good": 0.05, "poor": 0.35, "label": "Poor Teams calls", "unit": "share"},
        "vpn_failure":        {"category": "network", "agg": "per_day", "good": 0, "poor": 1.0, "label": "VPN failures", "unit": "/day"},
        "battery_health_pct": {"category": "hardware", "agg": "device", "good": 80, "poor": 50, "label": "Battery health", "unit": "%"},
        "ram_gb":             {"category": "hardware", "agg": "device", "good": 16, "poor": 4, "label": "RAM", "unit": "GB"},
        "sentiment":          {"category": "sentiment", "agg": "sentiment", "good": 4.5, "poor": 1.5, "label": "Survey answer", "unit": "/5"},
    },
    "caps": {
        "system_crash": 5.0,          # any blue screen in the window: the device score cannot be above this
        "very_poor_category": 3.0,    # a category below this...
        "category_margin": 3.0,       # ...caps the device at that category score + this margin
    },
    "min_teams_calls": 3,             # fewer calls than this in a window: the Teams metric is left out
    "bands": [4, 6, 8],               # score bands for the ticket correlation: <4, 4-6, 6-8, >=8
    "fixes": {"min_devices": 3, "battery_replace_below": 60, "disk_low_below": 10, "show": 10},
}


def load_config(path: Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path is None:
        return cfg
    try:
        import tomllib
    except ModuleNotFoundError as exc:          # Python 3.10
        raise SystemExit("A TOML config needs Python 3.11+ (tomllib). Remove --config to use the defaults.") from exc
    with open(path, "rb") as fh:
        _merge(cfg, tomllib.load(fh))
    return cfg


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def metric_score(value: float, good: float, poor: float) -> float:
    """10 at `good` or better, 0 at `poor` or worse, linear in between. Works whichever way the metric points:
    boot time (good 60 < poor 180) and Wi-Fi signal (good 70 > poor 40) use the same formula."""
    if good == poor:
        return 10.0 if value == good else 0.0
    t = (value - poor) / (good - poor)
    return round(10 * min(1.0, max(0.0, t)), 2)


def windows(now: datetime, weeks: int, days: int = 7) -> list[tuple[datetime, datetime]]:
    """`weeks` windows of `days` days, oldest first. The last one ends at midnight UTC before `now`, so a report
    run on Monday morning scores the full previous week and not two hours of Monday."""
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(weeks, 0, -1):
        e = end - timedelta(days=days * (i - 1))
        out.append((e - timedelta(days=days), e))
    return out


def device_metrics(dev: Device, evs: list[Event], answers: list[Answer], cfg: dict,
                   collected: set[str]) -> tuple[dict[str, float], dict]:
    """Raw metric values for one device in one window, plus details (which apps crash, which stop codes).
    Returns ({}, {}) when the device sent nothing in the window: no data is not a perfect score."""
    if not evs:
        return {}, {}
    by: dict[str, list[Event]] = defaultdict(list)
    for e in evs:
        by[e.type].append(e)
    active = len({e.ts.date() for e in evs})
    m: dict[str, float] = {}
    for name, spec in cfg["metrics"].items():
        agg = spec["agg"]
        if agg in ("median", "mean"):
            vals = [x for x in (e.number for e in by[name]) if x is not None]
            if vals:
                m[name] = statistics.median(vals) if agg == "median" else statistics.fmean(vals)
        elif agg == "per_day":
            if name in collected:               # a type no device reports is left out, not counted as zero
                m[name] = len(by[name]) / active
        elif agg == "minutes_per_day":
            if name in collected:
                m[name] = sum(e.number or 0 for e in by[name]) / active
        elif agg == "poor_ratio":
            calls = by[name]
            if len(calls) >= cfg.get("min_teams_calls", 1):
                m[name] = sum(1 for e in calls if str(e.value).lower() == "poor") / len(calls)
        elif agg == "device":
            v = getattr(dev, name, None)
            if v is not None:
                m[name] = v
        elif agg == "sentiment":
            if answers:
                m[name] = statistics.fmean(a.answer for a in answers)
    detail = {"active_days": active,
              "apps": Counter(str(e.value) for e in by["app_crash"] + by["app_hang"]),
              "bugchecks": Counter(str(e.value) for e in by["system_crash"]),
              "system_crashes": len(by["system_crash"])}
    return m, detail


@dataclass
class DeviceScore:
    device: str
    score: float
    categories: dict[str, float]
    metrics: dict[str, float]
    metric_scores: dict[str, float]
    capped: str = ""
    detail: dict = field(default_factory=dict)
    losses: dict[str, float] = field(default_factory=dict)   # points of device score each metric costs

    @property
    def main_issue(self) -> str:
        telemetry = {k: v for k, v in self.losses.items() if k != "sentiment"}    # the cause, not the feeling
        return max(telemetry, key=telemetry.get) if telemetry else ""


def combine(metrics: dict[str, float], cfg: dict, system_crashes: int = 0) -> tuple[float, dict, dict, str, dict]:
    """Metric values -> (device score, category scores, metric scores, cap applied, loss per metric)."""
    ms = {k: metric_score(v, cfg["metrics"][k]["good"], cfg["metrics"][k]["poor"]) for k, v in metrics.items()}
    cats: dict[str, float] = {}
    cat_w: dict[str, float] = {}
    for cat in CATEGORIES:
        names = [k for k in ms if cfg["metrics"][k]["category"] == cat]
        w = {k: cfg["metrics"][k].get("weight", 1) for k in names}
        if names and sum(w.values()) > 0:        # a category with no data is left out, not scored 0
            cats[cat] = sum(ms[k] * w[k] for k in names) / sum(w.values())
            cat_w[cat] = cfg["categories"][cat]["weight"]
    if not cats:
        return 0.0, {}, ms, "no data", {}
    total_w = sum(cat_w.values())
    score = sum(cats[c] * cat_w[c] for c in cats) / total_w
    losses = {}
    for k, s in ms.items():
        cat = cfg["metrics"][k]["category"]
        in_cat = sum(cfg["metrics"][n].get("weight", 1) for n in ms if cfg["metrics"][n]["category"] == cat)
        losses[k] = (10 - s) * cfg["metrics"][k].get("weight", 1) / in_cat * cat_w[cat] / total_w
    capped = ""
    caps = cfg["caps"]
    measured = {c: v for c, v in cats.items() if c != "sentiment"}   # one survey answer never caps a device
    worst = min(measured or cats, key=cats.get)
    if worst != "sentiment" and cats[worst] < caps["very_poor_category"] and score > cats[worst] + caps["category_margin"]:
        score = cats[worst] + caps["category_margin"]
        capped = f"very poor {cfg['categories'][worst]['label'].lower()}"
    if system_crashes and score > caps["system_crash"]:
        score = caps["system_crash"]
        capped = "system crash"
    return round(score, 2), {c: round(v, 2) for c, v in cats.items()}, ms, capped, losses


def score_device(dev: Device, evs: list[Event], answers: list[Answer], cfg: dict,
                 collected: set[str]) -> DeviceScore | None:
    m, detail = device_metrics(dev, evs, answers, cfg, collected)
    if not m:
        return None
    score, cats, ms, capped, losses = combine(m, cfg, detail.get("system_crashes", 0))
    return DeviceScore(dev.name, score, cats, m, ms, capped, detail,
                       {k: v for k, v in losses.items() if v >= 0.05})


def collected_types(ds: Dataset) -> set[str]:
    return {e.type for e in ds.events}


class Scores:
    """Scores for every device in every window, and the roll-ups the report needs."""

    def __init__(self, ds: Dataset, cfg: dict | None = None, now: datetime | None = None):
        self.ds, self.cfg = ds, cfg or copy.deepcopy(DEFAULT_CONFIG)
        self.now = now or ds.now
        self.windows = windows(self.now, self.cfg["weeks"], self.cfg["window_days"])
        self.collected = collected_types(ds)
        self.by_device: dict[str, list[Event]] = defaultdict(list)
        for e in ds.events:
            self.by_device[e.device].append(e)
        for evs in self.by_device.values():
            evs.sort(key=lambda e: e.ts)
        self.answers: dict[str, list[Answer]] = defaultdict(list)
        for a in ds.answers:
            self.answers[a.device].append(a)
        self.weeks: list[dict[str, DeviceScore]] = [self._score_window(i) for i in range(len(self.windows))]

    def events_in(self, device: str, week: int = -1) -> list[Event]:
        start, end = self.windows[week]
        evs = self.by_device.get(device, [])
        ts = [e.ts for e in evs]
        return evs[bisect_left(ts, start):bisect_left(ts, end)]

    def answers_in(self, device: str, week: int = -1) -> list[Answer]:
        start, end = self.windows[week]
        return [a for a in self.answers.get(device, []) if start.date() <= a.day < end.date()]

    def _score_window(self, i: int) -> dict[str, DeviceScore]:
        out = {}
        for name, dev in self.ds.devices.items():
            s = score_device(dev, self.events_in(name, i), self.answers_in(name, i), self.cfg, self.collected)
            if s:
                out[name] = s
        return out

    # roll-ups ---------------------------------------------------------------------------------------------
    @property
    def current(self) -> dict[str, DeviceScore]:
        return self.weeks[-1]

    @property
    def previous(self) -> dict[str, DeviceScore]:
        return self.weeks[-2] if len(self.weeks) > 1 else {}

    def fleet(self, week: int = -1) -> float | None:
        s = [d.score for d in self.weeks[week].values()]
        return round(statistics.fmean(s), 2) if s else None

    def trend(self) -> list[float | None]:
        return [self.fleet(i) for i in range(len(self.weeks))]

    def week_change(self) -> float | None:
        a, b = self.fleet(-1), self.fleet(-2) if len(self.weeks) > 1 else None
        return None if a is None or b is None else round(a - b, 2)

    def category_means(self, week: int = -1) -> dict[str, float]:
        out = {}
        for c in CATEGORIES:
            vals = [d.categories[c] for d in self.weeks[week].values() if c in d.categories]
            if vals:
                out[c] = round(statistics.fmean(vals), 2)
        return out

    def groups(self, attr: str, week: int = -1) -> list[dict]:
        """Site or department roll-up: the mean of its device scores (each device counts once)."""
        members: dict[str, list[str]] = defaultdict(list)
        for name, dev in self.ds.devices.items():
            members[getattr(dev, attr) or "(none)"].append(name)
        rows = []
        for g, names in members.items():
            trend = []
            for i in range(len(self.weeks)):
                vals = [self.weeks[i][n].score for n in names if n in self.weeks[i]]
                trend.append(round(statistics.fmean(vals), 2) if vals else None)
            cur = [self.weeks[week][n] for n in names if n in self.weeks[week]]
            if not cur:
                continue
            cats = {c: statistics.fmean(v) for c in CATEGORIES if c != "sentiment"
                    and (v := [d.categories[c] for d in cur if c in d.categories])}
            prev = trend[-2] if len(trend) > 1 else None
            rows.append({"name": g, "devices": len(cur), "score": trend[week],
                         "change": None if prev is None else round(trend[week] - prev, 2),
                         "below6": sum(1 for d in cur if d.score < 6), "trend": trend,
                         "weakest": min(cats, key=cats.get) if cats else ""})
        return sorted(rows, key=lambda r: r["score"])


def band(score: float, bands: list[float]) -> str:
    lo = None
    for b in bands:
        if score < b:
            return f"< {b}" if lo is None else f"{lo}–{b}"
        lo = b
    return f"≥ {bands[-1]}"


def band_labels(bands: list[float]) -> list[str]:
    labels = [f"< {bands[0]}"] + [f"{a}–{b}" for a, b in zip(bands, bands[1:])] + [f"≥ {bands[-1]}"]
    return labels


def describe(name: str, value: float, cfg: dict) -> str:
    """Human label for a metric value: 'Boot time 212 s', 'Poor Teams calls 40%', 'App crashes 1.4/day'."""
    spec = cfg["metrics"][name]
    unit, label = spec.get("unit", ""), spec["label"]
    if unit == "share":
        return f"{label} {value * 100:.0f}%"
    if unit == "/day":
        return f"{label} {value:.1f}/day"
    if unit == "/5":
        return f"{label} {value:.1f}/5"
    if unit == "%":
        return f"{label} {value:.0f}%"
    return f"{label} {value:.0f} {unit}".strip()


def issue_text(s: DeviceScore, cfg: dict) -> str:
    """The metric that costs this device most, with the app or stop code behind it when there is one."""
    k = s.main_issue
    if not k:
        return ""
    text = describe(k, s.metrics[k], cfg)
    if k in ("app_crash", "app_hang") and s.detail.get("apps"):
        text += f" ({s.detail['apps'].most_common(1)[0][0]})"
    elif k == "system_crash" and s.detail.get("bugchecks"):
        text += f" ({s.detail['bugchecks'].most_common(1)[0][0]})"
    return text
