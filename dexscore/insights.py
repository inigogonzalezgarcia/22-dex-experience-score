"""What to do with the scores: top fixes (what-if re-scoring), score vs tickets, VIP devices, worst devices.

A fix is estimated by re-scoring the affected devices as if the problem were gone (crash events removed, Wi-Fi
signal at the "good" threshold, a new battery...) and summing the difference. Only the metrics the fix directly
touches are reset, so the estimates are on the conservative side: better Wi-Fi would probably also improve
Teams calls, but that is not counted.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Callable

from .model import Device, Event
from .score import Scores, band, band_labels, metric_score, score_device

Transform = Callable[[Device, list[Event]], tuple[Device, list[Event]]]


@dataclass
class Fix:
    issue: str
    kind: str                  # app, bugcheck, wifi, vpn, refresh, boot, battery, disk
    scope: str                 # "fleet" or the site / model the fix is about
    devices: int
    fleet_gain: float          # change of the fleet score (mean of all devices) if fixed
    device_gain: float         # average change per affected device
    scope_gain: float | None   # change of the site score, for site-bound fixes
    action: str
    trend: str = ""            # new / growing / steady / shrinking, against the previous window
    names: list[str] = field(default_factory=list, repr=False)
    transform: Transform | None = field(default=None, repr=False)

    @property
    def gain_text(self) -> str:
        if self.scope_gain is not None and self.scope != "fleet":
            return f"+{self.scope_gain:.2f} {self.scope.split(' floor')[0]} score · +{self.fleet_gain:.2f} fleet"
        return f"+{self.fleet_gain:.2f} fleet score"


ACTIONS = {
    "app": "Read the faulting module in Application event 1000 on two or three of these devices. A shared module "
           "(an add-in, a plug-in, a DLL from a recent update) points to the cause: disable the add-in by policy "
           "or roll back the update for a pilot group first.",
    "bugcheck": "Same model, same stop code: compare drivers installed recently (display, Wi-Fi, chipset, dock "
                "firmware), pause that driver in the update ring and roll back on one device to confirm. Send the "
                "minidumps to the hardware vendor.",
    "wifi": "Survey the floor (access point load, channel overlap, signal at the desks) and read the controller logs "
            "for those access points. Compare with the same laptop model on other floors to rule out the driver.",
    "vpn": "Match the failure times with the VPN gateway logs: certificate expiry, client version, gateway capacity "
           "at peak hours. Start with the error codes the VPN client logged on two or three devices.",
    "boot": "Check startup apps, services and the disk on a sample. If the model is out of warranty, put it first "
            "in the next hardware refresh.",
    "refresh": "Old hardware on every count: slow start, worn battery, little RAM. Put these devices first in the "
               "hardware refresh; until then, check startup apps and free disk on each.",
    "battery": "Replace the batteries, or the laptops if they are near end of life. Below 60% of design capacity "
               "is the usual 'my laptop dies in meetings' ticket.",
    "disk": "Clean up (temp files, old profiles, OneDrive Files On-Demand) or enlarge the disk. With under 10% "
            "free, feature updates fail and everything gets slower.",
}


def _drop(pred: Callable[[Event], bool]) -> Transform:
    return lambda dev, evs: (dev, [e for e in evs if not pred(e)])


def _clamp(types: dict[str, str], cfg: dict) -> Transform:
    """Set numeric values of the given types to their 'good' threshold when worse ('min' or 'max' direction)."""
    def t(dev, evs):
        out = []
        for e in evs:
            if e.type in types and e.number is not None:
                good = cfg["metrics"][e.type]["good"]
                v = min(e.number, good) if types[e.type] == "min" else max(e.number, good)
                e = Event(e.ts, e.device, e.type, v)
            out.append(e)
        return dev, out
    return t


def what_if(scores: Scores, names: list[str], transform: Transform, week: int = -1) -> dict[str, float]:
    """Score gain per device if `transform` is applied to its device record and events in that window."""
    gains = {}
    for n in names:
        before = scores.weeks[week].get(n)
        if before is None:
            continue
        dev, evs = transform(scores.ds.devices[n], scores.events_in(n, week))
        after = score_device(dev, evs, scores.answers_in(n, week), scores.cfg, scores.collected)
        gains[n] = (after.score if after else before.score) - before.score
    return gains


def candidates(scores: Scores, week: int = -1) -> list[tuple[str, str, str, list[str], Transform]]:
    """(kind, issue, scope, devices, transform) for every problem worth estimating in that window."""
    cfg, devs = scores.cfg, scores.ds.devices
    fx = cfg["fixes"]
    cur = scores.weeks[week]
    out = []
    app_devices: dict[str, set[str]] = defaultdict(set)
    bug_devices: dict[str, set[str]] = defaultdict(set)
    bug_codes: dict[str, Counter] = defaultdict(Counter)
    wifi_by_loc: dict[str, list[str]] = defaultdict(list)
    vpn, boot_by_model = [], defaultdict(list)
    for n, s in cur.items():
        for app in s.detail.get("apps", {}):
            app_devices[app].add(n)
        if s.detail.get("system_crashes"):
            bug_devices[devs[n].model].add(n)
            bug_codes[devs[n].model].update(s.detail["bugchecks"])
        if "wifi_signal_pct" in s.metrics:
            wifi_by_loc[devs[n].location].append(n)
        if s.metrics.get("vpn_failure"):
            vpn.append(n)
        if s.metric_scores.get("boot_seconds", 10) < 10 or s.metric_scores.get("logon_seconds", 10) < 10:
            boot_by_model[devs[n].model].append(n)
    for app, names in app_devices.items():
        out.append(("app", f"{app} crashes and hangs on {len(names)} devices", "fleet", sorted(names),
                    _drop(lambda e, a=app: e.type in ("app_crash", "app_hang") and str(e.value) == a)))
    for model, names in bug_devices.items():
        code = bug_codes[model].most_common(1)[0][0]
        out.append(("bugcheck", f"System crashes (stop code {code}) on {len(names)} {model} devices", "fleet",
                    sorted(names), _drop(lambda e: e.type == "system_crash")))
    for loc, names in wifi_by_loc.items():
        bad = [n for n in names if cur[n].metric_scores.get("wifi_signal_pct", 10) < 7
               or cur[n].metric_scores.get("wifi_disconnect", 10) < 7]
        if bad:
            t = _clamp({"wifi_signal_pct": "max"}, cfg)
            out.append(("wifi", f"Weak Wi-Fi in {loc} ({len(bad)} of {len(names)} laptops affected)", loc, sorted(names),
                        lambda d, e, t=t: t(d, [x for x in e if x.type != "wifi_disconnect"])))
    if vpn:
        out.append(("vpn", f"VPN connection failures on {len(vpn)} devices", "fleet", sorted(vpn),
                    _drop(lambda e: e.type == "vpn_failure")))
    refresh: set[str] = set()
    by_model: dict[str, list[str]] = defaultdict(list)
    for n, s in cur.items():
        if s.categories.get("hardware", 10) < 5 and s.metric_scores.get("boot_seconds", 10) < 5:
            by_model[devs[n].model].append(n)
    for model, names in by_model.items():
        if len(names) < fx["min_devices"]:
            continue
        refresh.update(names)
        boot = statistics.median(cur[n].metrics["boot_seconds"] for n in names)
        batt = [devs[n].battery_health_pct for n in names if devs[n].battery_health_pct is not None]
        ram = statistics.median(devs[n].ram_gb or 0 for n in names)
        detail = f"median boot {boot:.0f} s" + (f", battery {statistics.median(batt):.0f}%" if batt else "") + \
            (f", {ram:.0f} GB RAM" if ram else "")
        new_hw = _clamp({"boot_seconds": "min", "logon_seconds": "min", "cpu_high_minutes": "min",
                         "mem_high_minutes": "min"}, cfg)
        good = cfg["metrics"]
        out.append(("refresh", f"Replace {len(names)} {model} devices ({detail})", "fleet", sorted(names),
                    lambda d, e, t=new_hw: t(replace(
                        d, ram_gb=max(d.ram_gb or 0, good["ram_gb"]["good"]),
                        battery_health_pct=None if d.battery_health_pct is None else 100.0,
                        disk_free_pct=max(d.disk_free_pct or 0, good["disk_free_pct"]["good"])), e)))
    for model, names in boot_by_model.items():
        names = [n for n in names if n not in refresh]
        if not names:
            continue
        boots = [cur[n].metrics["boot_seconds"] for n in names if "boot_seconds" in cur[n].metrics]
        b = cfg["metrics"]["boot_seconds"]
        if not boots or metric_score(statistics.median(boots), b["good"], b["poor"]) >= 7:
            continue                            # only models that are slow as a whole, not a few slow mornings
        label = f", median boot {statistics.median(boots):.0f} s"
        out.append(("boot", f"Slow boot and logon on {len(names)} {model} devices{label}", "fleet", sorted(names),
                    _clamp({"boot_seconds": "min", "logon_seconds": "min"}, cfg)))
    weak = sorted(n for n in cur if (devs[n].battery_health_pct or 100) < fx["battery_replace_below"]
                  and n not in refresh)
    if weak:
        out.append(("battery", f"{len(weak)} laptops with battery health below {fx['battery_replace_below']}%",
                    "fleet", weak, lambda d, e: (replace(d, battery_health_pct=100.0), e)))
    full = sorted(n for n in cur if (devs[n].disk_free_pct if devs[n].disk_free_pct is not None else 100)
                  < fx["disk_low_below"] and n not in refresh)
    if full:
        out.append(("disk", f"{len(full)} devices with less than {fx['disk_low_below']}% free disk", "fleet", full,
                    lambda d, e: (replace(d, disk_free_pct=30.0), e)))
    return [c for c in out if len(c[3]) >= fx["min_devices"]]


def top_fixes(scores: Scores, limit: int | None = None) -> list[Fix]:
    """Problems ranked by fleet score gain, which is (average gain per device) x (devices affected) / fleet size."""
    fleet_n = len(scores.current) or 1
    prev_n = len(scores.previous) or 1
    fixes = []
    for kind, issue, scope, names, transform in candidates(scores):
        gains = {n: g for n, g in what_if(scores, names, transform).items() if g > 0.005}
        total = sum(gains.values())
        if len(gains) < scores.cfg["fixes"]["min_devices"] or total / fleet_n < 0.02:
            continue
        scope_gain = None
        if kind == "wifi":
            site = scores.ds.devices[names[0]].site
            in_site = [n for n in scores.current if scores.ds.devices[n].site == site]
            scope_gain = round(total / len(in_site), 2)
        prev = sum(what_if(scores, names, transform, -2).values()) / prev_n if len(scores.weeks) > 1 else 0
        fleet_gain = total / fleet_n
        if prev < fleet_gain * 0.3:
            trend = "new"
        elif fleet_gain > prev * 1.2:
            trend = "growing"
        elif fleet_gain < prev * 0.8:
            trend = "shrinking"
        else:
            trend = "steady"
        fixes.append(Fix(issue, kind, scope, len(gains), round(fleet_gain, 2), round(total / len(gains), 2),
                         scope_gain, ACTIONS[kind], trend, sorted(gains), transform))
    fixes.sort(key=lambda f: (-f.fleet_gain, f.issue))
    return fixes[:limit] if limit else fixes


def combined_score(scores: Scores, fixes: list[Fix]) -> float | None:
    """Fleet score if all the given fixes were done together (overlapping devices are counted once)."""
    if not scores.current:
        return None
    total = 0.0
    for n, before in scores.current.items():
        dev, evs = scores.ds.devices[n], scores.events_in(n)
        touched = False
        for f in fixes:
            if n in f.names and f.transform:
                dev, evs = f.transform(dev, evs)
                touched = True
        after = score_device(dev, evs, scores.answers_in(n), scores.cfg, scores.collected) if touched else before
        total += (after or before).score
    return round(total / len(scores.current), 2)


def category_drags(scores: Scores, top: int = 2) -> dict[str, list[tuple[str, int]]]:
    """Per category, the metrics that cost the fleet most points, with how many devices score under 5 on them."""
    cost: dict[str, float] = defaultdict(float)
    poor: Counter = Counter()
    for s in scores.current.values():
        for k, v in s.losses.items():
            cost[k] += v
        for k, v in s.metric_scores.items():
            if v < 5:
                poor[k] += 1
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for k in sorted(cost, key=cost.get, reverse=True):
        cat = scores.cfg["metrics"][k]["category"]
        if len(out[cat]) < top and cost[k] >= 0.5:
            out[cat].append((k, poor[k]))
    return out


def ticket_correlation(scores: Scores, weeks: int = 4) -> dict:
    """Tickets per device in the last `weeks` windows, by current score band, plus Pearson r and silent devices."""
    start = scores.windows[max(0, len(scores.windows) - weeks)][0]
    end = scores.windows[-1][1]
    per_dev = Counter(t.device for t in scores.ds.tickets if start <= t.opened_at < end)
    bands = scores.cfg["bands"]
    rows = {b: {"band": b, "devices": 0, "tickets": 0, "with_ticket": 0} for b in band_labels(bands)}
    xs, ys = [], []
    for n, s in scores.current.items():
        r = rows[band(s.score, bands)]
        r["devices"] += 1
        r["tickets"] += per_dev[n]
        r["with_ticket"] += 1 if per_dev[n] else 0
        xs.append(s.score)
        ys.append(per_dev[n])
    for r in rows.values():
        r["per_device"] = round(r["tickets"] / r["devices"], 2) if r["devices"] else None
    try:
        corr = round(statistics.correlation(xs, ys), 2)
    except (statistics.StatisticsError, ValueError):
        corr = None
    low = [n for n, s in scores.current.items() if s.score < 6]
    return {"rows": list(rows.values()), "pearson": corr, "weeks": weeks, "has_tickets": bool(scores.ds.tickets),
            "below6": len(low), "below6_silent": sum(1 for n in low if not per_dev[n]), "per_device": per_dev}


def vip_below(scores: Scores, limit: float = 7.0) -> list[tuple[Device, object]]:
    return sorted(((scores.ds.devices[n], s) for n, s in scores.current.items()
                   if scores.ds.devices[n].vip and s.score < limit), key=lambda x: x[1].score)


def worst_devices(scores: Scores, n: int = 15) -> list[tuple[Device, object]]:
    return [(scores.ds.devices[k], s) for k, s in sorted(scores.current.items(), key=lambda kv: kv[1].score)[:n]]


def biggest_drops(scores: Scores, n: int = 5) -> list[tuple[str, float, float]]:
    """Devices whose score fell most since the previous window: (device, before, now)."""
    prev = scores.previous
    drops = [(k, prev[k].score, s.score) for k, s in scores.current.items() if k in prev and s.score < prev[k].score]
    return sorted(drops, key=lambda d: d[2] - d[1])[:n]


def no_data(scores: Scores) -> list[str]:
    return sorted(n for n in scores.ds.devices if n not in scores.current)


def window_label(scores: Scores, week: int = -1) -> str:
    s, e = scores.windows[week]
    return f"{s:%Y-%m-%d} to {(e - timedelta(days=1)):%Y-%m-%d}"
