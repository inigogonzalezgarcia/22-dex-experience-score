# How the score works

A digital experience score tries to answer one question with one number: **how good is it to work on this
device this week?** This page explains how `dexscore` builds that number, what it deliberately leaves out, and
where the choices are arbitrary. Everything here is in `DEFAULT_CONFIG` in [`dexscore/score.py`](../dexscore/score.py)
and can be changed with a TOML file (see the end of this page).

This is a lab model, written to understand the idea. It is inspired by the public descriptions of DEX scores in
commercial tools, but it is not a copy of any vendor's formula and the thresholds are my own starting points.

## 1. One window per week

Scores are calculated per device for a **7-day window**. The last window ends at midnight UTC before the newest
event (or before `--now`), so a report run on Monday morning scores Monday to Sunday and not two hours of Monday.
The trend shows the last **6 windows**.

A device that sent nothing in a window gets **no score** for it (not zero, not ten). It is listed under data
quality instead.

## 2. Each metric becomes 0–10

Every metric has a **good** and a **poor** threshold. At good or better it scores 10, at poor or worse it scores 0,
and in between it is linear:

```
score = 10 × (value − poor) / (good − poor), clamped to 0..10
```

The same formula works whichever way the metric points: for boot time good (60) is below poor (180), for Wi-Fi
signal good (70) is above poor (40).

| Category | Metric | From | Good | Poor |
|---|---|---|---|---|
| Device performance | Boot time | median of `boot_seconds` | 60 s | 180 s |
| | Logon time | median of `logon_seconds` | 20 s | 90 s |
| | High CPU | `cpu_high_minutes` per active day | 15 min | 120 min |
| | Memory pressure | `mem_high_minutes` per active day | 15 min | 120 min |
| | Free disk | `disk_free_pct` in devices.csv | 25 % | 5 % |
| Stability | App crashes | `app_crash` per active day | 0 | 1.0 |
| | App hangs | `app_hang` per active day | 0 | 1.5 |
| | System crashes (weight 2) | `system_crash` per active day | 0 | 0.4 |
| Network & collaboration | Wi-Fi signal | mean of `wifi_signal_pct` | 70 % | 40 % |
| | Wi-Fi drops | `wifi_disconnect` per active day | 0 | 3 |
| | Latency | median of `network_latency_ms` | 60 ms | 250 ms |
| | Poor Teams calls | share of `teams_call` = poor (3+ calls) | 5 % | 35 % |
| | VPN failures | `vpn_failure` per active day | 0 | 1.0 |
| Hardware health | Battery health | `battery_health_pct` in devices.csv | 80 % | 50 % |
| | RAM | `ram_gb` in devices.csv | 16 GB | 4 GB |
| Sentiment (optional) | Survey answer | mean answer 1–5 in the window | 4.5 | 1.5 |

**Rates are per active day**: events divided by the number of days the device sent anything. A laptop that was
on for two days and crashed twice has the same rate as one that was on for five days and crashed five times.
Holidays do not make a device look better or worse.

Medians are used for durations so that one slow boot after a feature update does not dominate the week; the
mean is used for Wi-Fi signal because a laptop that is often in a weak spot should show it.

## 3. Missing data is left out, not scored

- A desktop on a cable has no battery and no Wi-Fi samples: those metrics are **ignored**, not scored 0 or 10.
- A category with no metrics at all for a device (no battery and no RAM value, no survey answer) is **left out**
  and the remaining category weights are re-normalised.
- An event-count metric (crashes, drops, VPN failures) counts as **zero** for a device that reported other events,
  but only if **at least one device in the data** reports that type. If your collector does not gather VPN
  failures at all, nobody gets a free 10 for VPN.
- Teams call quality needs at least 3 calls in the window; with fewer, one bad call would be 33 % poor.

## 4. Categories and the device score

A category score is the weighted mean of its available metric scores (all weight 1, system crashes weight 2).
The device score is the weighted mean of the available categories:

| Category | Weight |
|---|---|
| Device performance | 30 % |
| Stability | 30 % |
| Network & collaboration | 25 % |
| Hardware health | 15 % |
| Sentiment | 10 % (only when there are answers) |

### Caps: when an average would lie

A weighted mean hides one terrible area. A laptop with perfect performance and no usable Wi-Fi is not an 8 for
the person sitting in front of it. Two caps stop that:

1. **System crash cap**: any system crash (blue screen, hard reset) in the window caps the device at **5.0**.
   Losing unsaved work once in a week is enough to make it a bad week.
2. **Very poor category cap**: if a measured category is below **3**, the device cannot score more than that
   category **+ 3**. Sentiment never triggers this cap: one survey answer is too thin to override telemetry.

The report shows which cap applied ("capped by").

### Worked example

A laptop with boot 95 s, logon 25 s, 20 min/day high CPU, 18 % free disk, 1.4 app crashes and 0.4 hangs per
day, good Wi-Fi and latency, 10 % poor Teams calls, battery at 84 %, 16 GB RAM, no survey answer:

| Category | Metric scores | Category |
|---|---|---|
| Performance | boot 7.1, logon 9.3, CPU 9.5, memory 10, disk 6.5 | 8.48 |
| Stability | app crashes 0, hangs 7.3, system crashes 10 (×2) | 6.83 |
| Network | signal 10, drops 9.3, latency 10, Teams 8.3, VPN 10 | 9.53 |
| Hardware | battery 10, RAM 10 | 10.0 |

Device score = (8.48 × 0.30 + 6.83 × 0.30 + 9.53 × 0.25 + 10 × 0.15) / 1.00 = **8.48**. The main issue is the
app crashes. If the same laptop had one blue screen that week, it would be capped at **5.0**.

## 5. Sites, departments, fleet

A site, department or fleet score is the **mean of its device scores** in the window; each device counts once.
"Below 6" counts devices under 6. The week change is this window minus the previous one.

## 6. What drags the score down

For every device, each metric's cost is how many points of the device score it takes away:
`(10 − metric score) × its share of the category × the category's share of the device score`. The largest
cost is the device's **main issue**. Summed over the fleet, the costs show which metrics drag each category.

## 7. Top fixes: what-if re-scoring

For each candidate problem, the affected devices are **scored again as if the problem were gone**, and the
difference is the estimated gain:

| Problem | Grouped by | "Fixed" means |
|---|---|---|
| App crashes and hangs | application name | that app's crash and hang events removed |
| System crashes | device model | system crash events removed |
| Weak Wi-Fi | site and floor | Wi-Fi signal raised to the good threshold, drops removed |
| VPN failures | fleet | VPN failure events removed |
| Old hardware | model, devices with hardware < 5 and boot < 5 | new device: boot, logon, CPU, memory at good, new battery, 16 GB, free disk |
| Slow boot | model, when the model's median boot scores under 7 | boot and logon at good |
| Worn batteries | devices below 60 % | battery at 100 % |
| Low disk | devices below 10 % free | 30 % free |

Problems are ranked by **fleet gain** = sum of device gains / devices in the fleet, which is the same as
(average gain per affected device) × (devices affected) / fleet size. A problem needs at least 3 affected devices
and a fleet gain of 0.02 to be listed. Site-bound problems (Wi-Fi) also show the site gain.

The estimates are conservative: only the metrics the fix touches are reset. Better Wi-Fi would probably also
reduce poor Teams calls and latency, but that is not counted. Fixes that touch the same device are not additive;
the "after the top 5 fixes" tile applies them together to get the combined number.

The **trend** label compares the gain with the same fix in the previous window: *new* (was under 30 %),
*growing* (more than 20 % bigger), *shrinking*, or *steady*.

## 8. Score vs tickets

Devices are put in bands by this week's score (< 4, 4–6, 6–8, ≥ 8) and the tickets opened from each device in
the last 4 weeks are counted per band, with the Pearson correlation between score and ticket count. The line that
matters most is how many devices below 6 opened **no ticket**: those are the problems nobody reported.

## Known limits

- Thresholds are starting points, not benchmarks. Tune them to your fleet after a few weeks of data.
- Disk, battery and RAM come from devices.csv as a snapshot and are applied to every week of the trend.
- Weights are a judgement call. Changing network from 25 % to 40 % changes the ranking of sites.
- A score is a pointer to where to look, not a measurement of how people feel. The survey is there to check it.

## Changing thresholds and weights

Python 3.11+ reads a TOML file with only the values you want to change:

```toml
# dex.toml
[metrics.boot_seconds]
good = 45
poor = 150

[categories.network]
weight = 0.35

[caps]
system_crash = 4.0
```

```bash
python -m dexscore report devices.csv events.jsonl --config dex.toml
```

A full example with every key: [thresholds.example.toml](thresholds.example.toml).
