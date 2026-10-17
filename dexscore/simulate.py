"""Fictional demo fleet: 120 devices of "Contoso" in Madrid, Lisbon and Paris, six weeks of telemetry.

Everything is invented: device names, users, models (the "Fabrikam" laptops do not exist) and every number.
The data is deterministic (fixed DEMO_NOW and seed), so the demo report is the same on every run.

Problems placed on purpose, so the report has something to find:
- an Outlook add-in crash wave from week 4 on 22 laptops,
- weak Wi-Fi on Madrid floor 3 (signal, drops, latency, poor Teams calls),
- 10 old laptops (Fabrikam ProBook 14 G2) with slow boot and logon, 8 GB of RAM and worn batteries,
- blue screens (0x0000009F) on the Fabrikam UltraSlim 13 after a driver update in week 5,
- a spike of VPN failures for remote workers in the last two weeks,
- one VIP whose laptop is out of disk space and memory: slow start, Teams freezing, poor calls.
One truncated JSONL line is included, as a collector killed mid-write would leave it.
"""
from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_NOW = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)      # a Monday morning
WEEKS = 6
SITES = {"Madrid": (50, 4, "MAD"), "Lisbon": (40, 3, "LIS"), "Paris": (30, 2, "PAR")}
DEPARTMENTS = ["Finance", "Sales", "Engineering", "HR", "Operations", "Marketing"]
DEPT_WEIGHTS = [18, 26, 24, 10, 26, 16]
G4, G2, SLIM, TOWER = "Fabrikam ProBook 14 G4", "Fabrikam ProBook 14 G2", "Fabrikam UltraSlim 13", "Fabrikam Tower 5"
FIRST = ["Lucia", "Hugo", "Marta", "Pablo", "Ines", "Joao", "Ana", "Tiago", "Claire", "Louis", "Camille", "Nicolas",
         "Elena", "Diego", "Sofia", "Rui", "Beatriz", "Julien", "Laura", "Alvaro", "Carla", "Manon", "Pedro", "Irene"]
LAST = ["Romero", "Navarro", "Gil", "Serrano", "Silva", "Santos", "Ferreira", "Costa", "Martin", "Bernard", "Dubois",
        "Moreau", "Ortega", "Delgado", "Rocha", "Lopes", "Laurent", "Petit", "Castro", "Vidal", "Almeida", "Garnier"]
APPS = ["ms-teams.exe", "EXCEL.EXE", "chrome.exe", "msedge.exe", "WINWORD.EXE", "Acrobat.exe"]


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def build_fleet(rng: random.Random) -> list[dict]:
    fleet, used = [], set()
    for site, (count, floors, code) in SITES.items():
        for i in range(count):
            desktop = i % 5 == 4                                   # one in five is a desktop on a cable
            floor = str(1 + i % floors) if site != "Madrid" else "3132431243"[i % 10]   # Madrid floor 3 is busy
            while True:
                user = f"{rng.choice(FIRST)}.{rng.choice(LAST)}".lower()
                if user not in used:
                    used.add(user)
                    break
            fleet.append({"device": f"{code}-{'D' if desktop else 'L'}-{i + 1:03d}", "user": user,
                          "department": rng.choices(DEPARTMENTS, DEPT_WEIGHTS)[0], "site": site, "floor": floor,
                          "model": TOWER if desktop else G4, "desktop": desktop, "remote": False, "outlook": False,
                          "vip": False, "vip_bad": False})
    laptops = [d for d in fleet if not d["desktop"]]
    rng.shuffle(laptops)
    pool = iter(laptops)
    vips = [next(pool) for _ in range(6)]
    for d in vips:
        d["vip"] = True
        d["department"] = rng.choice(["Finance", "Sales", "Operations"])
    vips[0]["vip_bad"] = True                                      # the VIP with the bad device
    old = [next(pool) for _ in range(10)]
    for d in old:
        d["model"] = G2
    slim = [next(pool) for _ in range(14)]
    for d in slim:
        d["model"] = SLIM
    rest = [d for d in pool]
    for d in [d for d in rest if d["department"] in ("Sales", "Operations")][:24]:
        d["remote"] = True                                         # works from home a lot, on VPN
    outlook = rng.sample([d for d in rest if d["site"] != "Madrid" or d["floor"] != "3"], 22)
    for d in outlook:
        d["outlook"] = True
    for d in fleet:
        m = d["model"]
        d["ram_gb"] = 8 if m == G2 else (32 if m == TOWER and d["department"] == "Engineering" else 16)
        d["disk_free_pct"] = round(rng.uniform(8, 20) if m == G2 else rng.uniform(12, 62), 1)
        # every device is a bit different: its own boot time, load and how often its apps fall over
        d["boot_base"] = rng.uniform(30, 85) * (0.85 if m == SLIM else 1)
        d["logon_base"] = rng.uniform(8, 32)
        d["load"] = rng.uniform(0.05, 0.6)
        d["crashy"] = rng.uniform(0.01, 0.08)
        d["lat_base"] = rng.uniform(18, 70)
        d["wifi_base"] = rng.uniform(52, 86)
        d["poor_calls"] = rng.uniform(0.0, 0.12)
        d["battery_health_pct"] = None if d["desktop"] else round(
            rng.uniform(38, 58) if m == G2 else rng.uniform(78, 98), 1)
        d["os_build"] = "22631.5909" if m == G2 else "26100.6584"
        if d["vip_bad"]:
            d["disk_free_pct"] = 3.5
    return fleet


def _events_for_day(rng: random.Random, d: dict, day: datetime, week: int) -> list[dict]:
    """All events one device sends on one workday. `week` is 1 (oldest) to 6 (last full week)."""
    ev = []

    def add(typ, value):
        ts = day + timedelta(hours=rng.uniform(7.5, 18.5))
        ev.append({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "device": d["device"], "type": typ, "value": value})

    m, f3 = d["model"], d["site"] == "Madrid" and d["floor"] == "3" and not d["desktop"]
    if rng.random() < (0.5 if d["desktop"] else 0.7):
        boot = rng.uniform(150, 240) if m == G2 else rng.gauss(125 if d["vip_bad"] else d["boot_base"], 9)
        add("boot_seconds", round(max(15, boot)))
        logon = rng.uniform(60, 110) if m == G2 else rng.gauss(65 if d["vip_bad"] else d["logon_base"], 4)
        add("logon_seconds", round(max(5, logon)))
    for _ in range(rng.randint(1, 2)):
        lat = rng.gauss(95, 35) if f3 else rng.gauss(60, 15) if d["remote"] else rng.gauss(d["lat_base"], 8)
        add("network_latency_ms", round(max(5, lat)))
    if not d["desktop"]:
        for _ in range(2):
            add("wifi_signal_pct", round(min(99, rng.uniform(22, 48) if f3 else rng.gauss(d["wifi_base"], 8))))
        for _ in range(_poisson(rng, 2.6 if f3 else 0.08)):
            add("wifi_disconnect", "")
    poor = 0.4 if f3 else 0.5 if d["vip_bad"] else 0.08 + d["poor_calls"] if d["remote"] else d["poor_calls"]
    for _ in range(rng.randint(1, 4) if d["vip"] else rng.randint(0, 3)):
        add("teams_call", "poor" if rng.random() < poor else "good")
    if d["vip_bad"]:
        add("cpu_high_minutes", round(rng.uniform(60, 140)))
        add("mem_high_minutes", round(rng.uniform(130, 210)))
    elif m == G2:
        add("cpu_high_minutes", round(rng.uniform(30, 80)))
        add("mem_high_minutes", round(rng.uniform(50, 110)))
    else:
        if rng.random() < d["load"]:
            add("cpu_high_minutes", round(rng.uniform(5, 60)))
        if rng.random() < d["load"] * 0.8:
            add("mem_high_minutes", round(rng.uniform(5, 50)))
    if rng.random() < d["crashy"]:
        add("app_crash", rng.choice(APPS))
    if rng.random() < d["crashy"] * 1.2:
        add("app_hang", rng.choice(APPS))
    if d["vip_bad"]:                                   # Teams freezes when the disk and memory are full
        for _ in range(_poisson(rng, 1.4)):
            add("app_hang", "ms-teams.exe")
    if d["outlook"] and week >= 4:                     # add-in update reaches the pilot ring, then everyone
        for _ in range(_poisson(rng, 1.6 if week >= 5 else 0.8)):
            add("app_crash", "OUTLOOK.EXE")
        for _ in range(_poisson(rng, 0.8 if week >= 5 else 0.4)):
            add("app_hang", "OUTLOOK.EXE")
    if m == SLIM and week >= 5 and rng.random() < 0.3:  # display driver pushed in week 5
        add("system_crash", "0x0000009f")
    elif rng.random() < 0.0015:
        add("system_crash", rng.choice(["0x000000ef", "0x0000007e"]))
    if d["remote"]:
        lam = {5: 0.35, 6: 1.6}.get(week, 0.04)
        for _ in range(_poisson(rng, lam)):
            add("vpn_failure", rng.choice(["809", "812", "13801"]))
    return ev


def _tickets_and_answers(rng: random.Random, d: dict, week: int, days: list[datetime]):
    tickets, answers = [], []
    f3 = d["site"] == "Madrid" and d["floor"] == "3" and not d["desktop"]
    issues = [("Software request", 0.06, 0)]
    if d["outlook"] and week >= 4:
        issues.append(("Email / Outlook", 0.35, 1.5))
    if f3:
        issues.append(("Network / Wi-Fi", 0.25, 1.4))
    if d["model"] == G2:
        issues.append(("Hardware / slow laptop", 0.15, 1.0))
    if d["model"] == SLIM and week >= 5:
        issues.append(("Blue screen", 0.45, 2.0))
    if d["remote"] and week >= 5:
        issues.append(("VPN", 0.15 if week == 5 else 0.35, 1.0))
    if d["vip_bad"]:
        issues.append(("Performance", 0.5, 2.0))
    for cat, p, _ in issues:
        if rng.random() < p:
            ts = rng.choice(days) + timedelta(hours=rng.uniform(8, 18))
            tickets.append({"opened_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "device": d["device"], "category": cat})
    if rng.random() < 0.45:                            # answered the weekly "How is your device today?" survey
        base = rng.choices([5, 4, 3, 2], [45, 40, 12, 3])[0]
        mood = base - sum(pen for _, _, pen in issues) * rng.uniform(0.6, 1.0)
        answers.append({"device": d["device"], "date": f"{rng.choice(days):%Y-%m-%d}",
                        "answer": int(min(5, max(1, round(mood))))})
    return tickets, answers


def write_demo(folder: Path) -> list[Path]:
    rng = random.Random(22)
    fleet = build_fleet(rng)
    end = DEMO_NOW.replace(hour=0, minute=0)
    start = end - timedelta(days=7 * WEEKS)
    events, tickets, answers = [], [], []
    for w in range(WEEKS):
        days = [start + timedelta(days=7 * w + i) for i in range(5)]            # Monday to Friday
        for d in fleet:
            for day in days:
                events += _events_for_day(rng, d, day, w + 1)
            t, a = _tickets_and_answers(rng, d, w + 1, days)
            tickets += t
            answers += a
    events.sort(key=lambda e: (e["ts"], e["device"]))
    folder.mkdir(parents=True, exist_ok=True)
    paths = {k: folder / f for k, f in (("devices", "devices.csv"), ("events", "events.jsonl"),
                                        ("tickets", "tickets.csv"), ("sentiment", "sentiment.csv"))}
    cols = ["device", "user", "department", "site", "floor", "model", "os_build", "ram_gb", "disk_free_pct",
            "battery_health_pct", "vip"]
    with open(paths["devices"], "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for d in sorted(fleet, key=lambda d: d["device"]):
            w.writerow([d["device"], d["user"], d["department"], d["site"], d["floor"], d["model"], d["os_build"],
                        d["ram_gb"], d["disk_free_pct"], "" if d["battery_health_pct"] is None else d["battery_health_pct"],
                        "yes" if d["vip"] else "no"])
    with open(paths["events"], "w", encoding="utf-8") as fh:
        for i, e in enumerate(events):
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")
            if i == len(events) // 2:
                fh.write('{"ts":"2026-09-30T11:02:17Z","device":"LIS-L-0\n')    # truncated on purpose
    for key, rows, header in (("tickets", sorted(tickets, key=lambda t: t["opened_at"]), ["opened_at", "device", "category"]),
                              ("sentiment", sorted(answers, key=lambda a: (a["date"], a["device"])), ["device", "date", "answer"])):
        with open(paths[key], "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header)
            w.writeheader()
            w.writerows(rows)
    return list(paths.values())
