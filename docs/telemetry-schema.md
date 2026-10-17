# Telemetry schema

`python -m dexscore report` takes any number of files and works out what each one is from its extension and
header: `.jsonl` files are events, a CSV with `opened_at` is tickets, a CSV with `answer` is the survey, any
other CSV with a `device` column is the device list. Several device CSVs (one per device, as the collector
writes them) and several event files are merged.

All files are UTF-8; a BOM (Excel, Windows PowerShell 5.1) is fine. Column names are not case-sensitive.
Device names are compared in upper case. Timestamps are ISO 8601; without a time zone they are taken as UTC.

## devices.csv (required)

One row per device. Later files win if a device appears twice.

| Column | Example | Notes |
|---|---|---|
| `device` | `MAD-L-031` | required |
| `user` | `carla.ferreira` | optional; leave empty if you do not need it (see privacy in the README) |
| `department` | `Operations` | used for the department roll-up |
| `site` | `Madrid` | used for the site roll-up |
| `floor` | `3` | optional; Wi-Fi problems are grouped by site and floor when present |
| `model` | `Fabrikam ProBook 14 G2` | system crashes and old hardware are grouped by model |
| `os_build` | `26100.6584` | informational |
| `ram_gb` | `16` | Hardware health |
| `disk_free_pct` | `12.5` | Device performance; `12,5` is accepted too |
| `battery_health_pct` | `46` | full charge / design capacity; **empty for desktops** |
| `vip` | `yes` / `no` | VIP section of the report |

## events.jsonl (required)

One JSON object per line: `ts`, `device`, `type`, `value`. Lines that are not valid JSON or miss `ts`, `device`
or `type` are skipped and counted under data quality. Types this version does not know are ignored and counted.

```json
{"ts":"2026-10-14T08:12:40Z","device":"MAD-L-031","type":"boot_seconds","value":212}
{"ts":"2026-10-14T10:03:11Z","device":"LIS-L-008","type":"system_crash","value":"0x0000009f"}
{"ts":"2026-10-14T11:20:05Z","device":"PAR-L-004","type":"app_crash","value":"OUTLOOK.EXE"}
{"ts":"2026-10-14T15:47:52Z","device":"MAD-L-006","type":"teams_call","value":"poor"}
```

| Type | Value | Used as |
|---|---|---|
| `boot_seconds` | seconds | median per window |
| `logon_seconds` | seconds | median per window |
| `cpu_high_minutes` | minutes above ~90 % CPU since the last sample | sum per active day |
| `mem_high_minutes` | minutes above ~90 % committed memory | sum per active day |
| `app_crash` | application name (`OUTLOOK.EXE`) | count per active day; grouped by app for fixes |
| `app_hang` | application name | count per active day |
| `system_crash` | stop code (`0x0000009f`) or `kernel-power-41` | count per active day; caps the device |
| `wifi_signal_pct` | 0–100 | mean per window |
| `wifi_disconnect` | anything (ignored) | count per active day |
| `network_latency_ms` | milliseconds | median per window |
| `teams_call` | `good` or `poor` | share of poor calls (3+ calls) |
| `vpn_failure` | error code (ignored) | count per active day |

An **active day** is a UTC date on which the device sent at least one event of any type.

## tickets.csv (optional)

Export from your ITSM tool, one row per ticket that has a device (configuration item) on it.

| Column | Example |
|---|---|
| `opened_at` | `2026-10-13T09:30:00Z` or `2026-10-13 09:30` |
| `device` | `MAD-L-006` |
| `category` | `Network / Wi-Fi` (informational) |

## sentiment.csv (optional)

Answers to a one-question pulse survey ("How is your device today?", 1 = very bad, 5 = great). Either
`device` or `user` must be filled; a user is mapped to their device(s) through devices.csv. Answers outside
1–5 are dropped.

| Column | Example |
|---|---|
| `device` | `MAD-L-006` |
| `user` | `joao.petit` |
| `date` | `2026-10-14` |
| `answer` | `2` |

## Where the data could come from

| Source | Fields |
|---|---|
| `scripts/Collect-DexTelemetry.ps1` | boot, logon (Group Policy), app crashes and hangs, system crashes, Wi-Fi drops, VPN failures (built-in client), Wi-Fi signal and latency samples, device row |
| Microsoft Intune Endpoint Analytics | boot and sign-in times, app reliability; export and map to the types above |
| Your RMM agent | CPU and memory pressure history, disk, battery |
| Teams admin center / Call Quality Dashboard | per-call quality (`teams_call`) |
| ITSM tool | tickets.csv |
| Microsoft Forms or a Teams poll | sentiment.csv |
