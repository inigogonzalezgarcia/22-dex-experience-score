import copy
import csv
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from dexscore import cli, insights, simulate
from dexscore.model import Answer, Dataset, Device, Event, Ticket, load, read_events
from dexscore.score import DEFAULT_CONFIG, Scores, combine, device_metrics, load_config, metric_score, windows

NOW = simulate.DEMO_NOW                     # Monday 07:00 UTC; the last full window is the 7 days before


def cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


def day(week_back=0, d=0, hour=10):
    """A timestamp `week_back` windows before the last one, on day `d` (0 = Monday) of that window."""
    base = NOW.replace(hour=0, minute=0) - timedelta(days=7 * (week_back + 1))
    return base + timedelta(days=d, hours=hour)


def healthy(name, week_back=0, days=5):
    """A device with good numbers on every workday of one window."""
    evs = []
    for d in range(days):
        evs += [Event(day(week_back, d), name, "boot_seconds", 40), Event(day(week_back, d), name, "logon_seconds", 12),
                Event(day(week_back, d), name, "network_latency_ms", 25), Event(day(week_back, d), name, "wifi_signal_pct", 80)]
    return evs


def dev(name, **kw):
    base = dict(name=name, department="IT", site="Madrid", model="M1", ram_gb=16, disk_free_pct=40, battery_health_pct=90)
    base.update(kw)
    return Device(**base)


class MetricMapping(unittest.TestCase):
    def test_good_poor_linear_and_inverted(self):
        self.assertEqual(metric_score(60, 60, 180), 10)          # boot: lower is better
        self.assertEqual(metric_score(30, 60, 180), 10)
        self.assertEqual(metric_score(180, 60, 180), 0)
        self.assertEqual(metric_score(120, 60, 180), 5)
        self.assertEqual(metric_score(55, 70, 40), 5)            # Wi-Fi signal: higher is better
        self.assertEqual(metric_score(90, 70, 40), 10)
        self.assertEqual(metric_score(10, 70, 40), 0)

    def test_rate_is_per_active_day(self):
        evs = [Event(day(0, d), "A", "boot_seconds", 40) for d in range(4)]      # active 4 days
        evs += [Event(day(0, 1), "A", "app_crash", "x.exe"), Event(day(0, 2), "A", "app_crash", "x.exe")]
        m, detail = device_metrics(dev("A"), evs, [], cfg(), {"boot_seconds", "app_crash"})
        self.assertEqual(detail["active_days"], 4)
        self.assertAlmostEqual(m["app_crash"], 0.5)
        self.assertNotIn("vpn_failure", m)                       # not collected by anyone: left out, not 0


class Combining(unittest.TestCase):
    def test_category_weighting(self):
        c = cfg()
        # performance 10 (weight .30), network 0 (weight .25): 10*.30/(.30+.25)
        score, cats, _, capped, _ = combine({"boot_seconds": 30, "network_latency_ms": 2000}, c)
        self.assertEqual(cats, {"performance": 10.0, "network": 0.0})
        # the network category is very poor, so the cap (0 + 3) applies instead of the mean 5.45
        self.assertEqual((score, capped), (3.0, "very poor network & collaboration"))
        score, _, _, capped, _ = combine({"boot_seconds": 30, "network_latency_ms": 155}, c)   # network 5
        self.assertEqual((score, capped), (round((10 * .30 + 5 * .25) / .55, 2), ""))

    def test_system_crash_caps_the_device(self):
        c = cfg()
        m = {"boot_seconds": 30, "app_crash": 0, "app_hang": 0, "system_crash": 0.2}
        score, cats, _, capped, _ = combine(m, c, system_crashes=1)
        self.assertGreater(cats["stability"], 5)                 # the mean alone would stay high
        self.assertEqual((score, capped), (5.0, "system crash"))

    def test_missing_battery_is_ignored_not_zero(self):
        c = cfg()
        evs = healthy("D")
        desk = dev("D", battery_health_pct=None, ram_gb=None)
        m, _ = device_metrics(desk, evs, [], c, {e.type for e in evs})
        self.assertNotIn("battery_health_pct", m)
        _, cats, _, _, _ = combine(m, c)
        self.assertNotIn("hardware", cats)                       # no hardware data: category left out
        m2, _ = device_metrics(dev("D", battery_health_pct=None), evs, [], c, {e.type for e in evs})
        self.assertEqual(combine(m2, c)[1]["hardware"], 10.0)    # RAM only, battery not counted as 0

    def test_sentiment_is_optional(self):
        devices = {"A": dev("A")}
        without = Scores(Dataset(devices, healthy("A")), cfg(), NOW)
        self.assertNotIn("sentiment", without.current["A"].categories)
        unhappy = [Answer(day(0, 2).date(), "A", 1)]
        withs = Scores(Dataset(devices, healthy("A"), answers=unhappy), cfg(), NOW)
        self.assertEqual(withs.current["A"].categories["sentiment"], 0)
        self.assertLess(withs.current["A"].score, without.current["A"].score)
        self.assertEqual(withs.current["A"].capped, "")          # one survey answer never caps a device


class RollUps(unittest.TestCase):
    def test_site_and_department_are_means_of_devices(self):
        devices = {"A": dev("A", site="Madrid", department="Sales"), "B": dev("B", site="Madrid", department="HR"),
                   "C": dev("C", site="Lisbon", department="Sales")}
        evs = healthy("A") + healthy("C") + [e for e in healthy("B") if e.type != "boot_seconds"]
        evs += [Event(day(0, d), "B", "boot_seconds", 120) for d in range(5)]   # B boots slowly
        sc = Scores(Dataset(devices, evs), cfg(), NOW)
        sites = {g["name"]: g for g in sc.groups("site")}
        self.assertEqual(sites["Madrid"]["devices"], 2)
        self.assertAlmostEqual(sites["Madrid"]["score"], round((sc.current["A"].score + sc.current["B"].score) / 2, 2))
        depts = {g["name"]: g["score"] for g in sc.groups("department")}
        self.assertEqual(depts["HR"], sc.current["B"].score)
        self.assertEqual(sc.groups("site")[0]["name"], "Madrid")   # worst first

    def test_week_bucketing_and_trend(self):
        w = windows(NOW, 6)
        self.assertEqual(len(w), 6)
        self.assertEqual(w[-1][1], NOW.replace(hour=0, minute=0))        # ends at midnight before "now"
        self.assertEqual(w[0][0], w[-1][1] - timedelta(days=42))
        devices = {"A": dev("A")}
        evs = healthy("A", 2) + healthy("A", 1) + [e for e in healthy("A", 0) if e.type != "boot_seconds"]
        evs += [Event(day(0, d), "A", "boot_seconds", 180) for d in range(5)]
        evs.append(Event(NOW, "A", "boot_seconds", 999))                  # today: outside every window
        sc = Scores(Dataset(devices, evs), cfg(), NOW)
        t = sc.trend()
        self.assertEqual(t[:3], [None, None, None])                       # no data is not a score
        self.assertEqual(t[3], t[4])
        self.assertLess(t[5], t[4])
        self.assertEqual(sc.week_change(), round(t[5] - t[4], 2))


class NoData(unittest.TestCase):
    def test_silent_device_is_not_scored(self):
        devices = {"A": dev("A"), "B": dev("B")}
        evs = healthy("A") + healthy("B", week_back=1)       # B went quiet this week
        sc = Scores(Dataset(devices, evs), cfg(), NOW)
        self.assertEqual(sorted(sc.current), ["A"])
        self.assertEqual(insights.no_data(sc), ["B"])
        self.assertEqual(sc.fleet(), sc.current["A"].score)  # B is not averaged in as a zero
        self.assertIn("B", sc.previous)


class Fixes(unittest.TestCase):
    def test_ranking_by_fleet_gain(self):
        devices = {f"D{i}": dev(f"D{i}") for i in range(10)}
        evs = []
        for i in range(10):
            evs += healthy(f"D{i}")
        for i in range(6):                     # Outlook crashes on 6 devices, twice a day
            evs += [Event(day(0, d, 11), f"D{i}", "app_crash", "OUTLOOK.EXE") for d in range(5) for _ in range(2)]
        for i in range(6, 9):                  # Excel crashes on 3 devices, once a day
            evs += [Event(day(0, d, 12), f"D{i}", "app_crash", "EXCEL.EXE") for d in range(5)]
        evs.append(Event(day(0, 1), "D9", "app_crash", "notepad.exe"))  # a single crash is not a fix
        sc = Scores(Dataset(devices, evs), cfg(), NOW)
        fixes = insights.top_fixes(sc)
        self.assertEqual([f.issue for f in fixes], ["OUTLOOK.EXE crashes and hangs on 6 devices",
                                                     "EXCEL.EXE crashes and hangs on 3 devices"])
        self.assertGreater(fixes[0].fleet_gain, fixes[1].fleet_gain)
        self.assertEqual(fixes[0].devices, 6)
        self.assertGreater(insights.combined_score(sc, fixes), sc.fleet())

    def test_tickets_correlation_and_vip(self):
        devices = {"A": dev("A", vip=True), "B": dev("B"), "C": dev("C")}
        evs = healthy("A") + healthy("B") + healthy("C")
        evs += [Event(day(0, d), "A", "network_latency_ms", 2000) for d in range(5) for _ in range(3)]
        evs += [Event(day(0, d), "A", "wifi_signal_pct", 10) for d in range(5) for _ in range(3)]
        tickets = [Ticket(day(1, 2), "A", "Network"), Ticket(day(0, 3), "A", "Network"), Ticket(day(9), "B", "old")]
        sc = Scores(Dataset(devices, evs, tickets=tickets), cfg(), NOW)
        tc = insights.ticket_correlation(sc)
        rows = {r["band"]: r for r in tc["rows"]}
        low = rows["< 4"] if rows["< 4"]["devices"] else rows["4–6"]
        self.assertEqual((low["devices"], low["tickets"]), (1, 2))      # the ticket 10 weeks ago is not counted
        self.assertEqual(rows["≥ 8"]["tickets"], 0)
        self.assertLess(tc["pearson"], 0)
        self.assertEqual([d.name for d, _ in insights.vip_below(sc)], ["A"])


class Formats(unittest.TestCase):
    def test_jsonl_bad_lines_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('{"ts":"2026-10-12T08:00:00.1234567Z","device":"pc1","type":"boot_seconds","value":50}\n'
                         '\n{"ts":"2026-10-12T08:00:00Z","device":"PC1","type":"boot_sec\n'
                         'not json\n{"device":"PC1","type":"app_crash"}\n'
                         '{"ts":"2026-10-12T09:00:00+02:00","device":"PC1","type":"printer_jam","value":1}\n'
                         '{"ts":"2026-10-12T09:00:00+02:00","device":"PC1","type":"app_crash","value":"a.exe"}\n',
                         encoding="utf-8")
            evs, bad, unknown = read_events(p)
        self.assertEqual((len(evs), bad, unknown), (2, 3, 1))
        self.assertEqual(evs[0].device, "PC1")
        self.assertEqual(evs[0].ts.microsecond, 123456)
        self.assertEqual(evs[1].ts.hour, 7)                     # converted to UTC

    def test_csv_with_bom_and_file_sniffing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "inventory.csv").write_text("﻿Device,User,Department,Site,Model,RAM_GB,Disk_Free_Pct,"
                                             "Battery_Health_Pct,VIP\npc1,ana,HR,Paris,M,16,\"12,5\",,Yes\n",
                                             encoding="utf-8")
            (t / "e.jsonl").write_text('{"ts":"2026-10-12T08:00:00Z","device":"PC1","type":"boot_seconds","value":50}\n'
                                       '{"ts":"2026-10-12T08:00:00Z","device":"PC9","type":"boot_seconds","value":50}\n',
                                       encoding="utf-8")
            (t / "PC2-device.csv").write_text('\ufeff"device","model","ram_gb","vip"\n"PC2","M","8","no"\n',
                                              encoding="utf-8")       # as Export-Csv in PowerShell 5.1 writes it
            (t / "itsm.csv").write_text("﻿opened_at,device,category\n2026-10-12 09:30,PC1,Wi-Fi\n", encoding="utf-8")
            (t / "survey.csv").write_text("user,date,answer\nANA,2026-10-12,2\nana,2026-10-13,9\n", encoding="utf-8")
            ds = load([t / "inventory.csv", t / "PC2-device.csv", t / "e.jsonl", t / "itsm.csv", t / "survey.csv"])
        d = ds.devices["PC1"]
        self.assertEqual((d.department, d.disk_free_pct, d.battery_health_pct, d.vip), ("HR", 12.5, None, True))
        self.assertEqual((ds.devices["PC2"].ram_gb, ds.devices["PC2"].vip), (8, False))
        self.assertEqual(len(ds.tickets), 1)
        self.assertEqual([(a.device, a.answer) for a in ds.answers], [("PC1", 2)])   # out-of-range answer dropped
        self.assertEqual(ds.unknown_devices, {"PC9"})

    @unittest.skipIf(sys.version_info < (3, 11), "TOML config needs tomllib (Python 3.11+)")
    def test_toml_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dex.toml"
            p.write_text("[metrics.boot_seconds]\ngood = 30\npoor = 90\n[categories.network]\nweight = 0.5\n")
            c = load_config(p)
        self.assertEqual((c["metrics"]["boot_seconds"]["good"], c["metrics"]["boot_seconds"]["poor"]), (30, 90))
        self.assertEqual(c["metrics"]["boot_seconds"]["category"], "performance")   # untouched keys kept
        self.assertEqual(c["categories"]["network"]["weight"], 0.5)
        self.assertEqual(DEFAULT_CONFIG["metrics"]["boot_seconds"]["good"], 60)     # defaults not mutated


class EndToEnd(unittest.TestCase):
    def test_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, data = Path(tmp) / "out", Path(tmp) / "data"
            self.assertEqual(cli.main(["demo", "--out", str(out), "--data", str(data)]), 0)
            for name in ("report.html", "device_scores.csv", "top_fixes.csv", "review.md"):
                self.assertTrue((out / name).stat().st_size > 500, name)
            with open(out / "top_fixes.csv", encoding="utf-8") as fh:
                top = [r["kind"] + " " + r["issue"] for r in csv.DictReader(fh)][:5]
            html = (out / "report.html").read_text(encoding="utf-8")
            review = (out / "review.md").read_text(encoding="utf-8")
            ds = load(sorted(data.iterdir()))
        expected = ["bugcheck", "refresh", "app OUTLOOK.EXE", "vpn", "wifi Weak Wi-Fi in Madrid floor 3"]
        for prefix in expected:                   # every planted problem is in the top 5
            self.assertTrue(any(t.startswith(prefix) for t in top), (prefix, top))
        self.assertIn("UltraSlim 13", " ".join(top))
        self.assertIn("ProBook 14 G2", " ".join(top))
        self.assertEqual(ds.bad_lines, 1)         # the truncated line placed on purpose
        self.assertEqual(len(ds.devices), 120)
        self.assertIn("<svg", html)
        self.assertNotIn("<script", html)
        self.assertIn("VIP devices below 7", review)
        sc = Scores(ds, cfg(), NOW)
        self.assertEqual(len(insights.vip_below(sc)), 1)        # the one VIP with a bad device
        t = sc.trend()
        self.assertLess(t[-1], t[2] - 0.5)       # the problems from week 4 on pull the fleet down


if __name__ == "__main__":
    unittest.main()
