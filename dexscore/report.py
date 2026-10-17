"""Outputs: report.html (inline SVG charts, light and dark), device_scores.csv, top_fixes.csv and review.md."""
from __future__ import annotations

import csv
import html
import math
from pathlib import Path

from . import insights as ins
from .score import CATEGORIES, Scores, band, describe, issue_text

e = html.escape


def fmt(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "–"
    return f"{v:+.2f}".replace("+0.00", "±0.00") if signed else f"{v:.1f}"


def cls(score: float | None) -> str:
    if score is None:
        return "muted"
    return "critical" if score < 4 else "warning" if score < 6 else "info" if score < 8 else "ok"


def summary(sc: Scores, fixes: list[ins.Fix]) -> dict:
    cur = sc.current
    return {"devices": len(sc.ds.devices), "scored": len(cur), "fleet": sc.fleet(), "change": sc.week_change(),
            "below6": sum(1 for s in cur.values() if s.score < 6), "events": len(sc.ds.events),
            "bad_lines": sc.ds.bad_lines, "top": fixes[0].issue if fixes else "",
            "after_top5": ins.combined_score(sc, fixes[:5]) if fixes else sc.fleet()}


# CSV ------------------------------------------------------------------------------------------------------------
def write_device_scores(sc: Scores, path: Path) -> None:
    tickets = ins.ticket_correlation(sc)["per_device"]
    prev = sc.previous
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["device", "user", "department", "site", "floor", "model", "vip", "score", "band",
                    *CATEGORIES, "change_vs_previous_week", "capped_by", "main_issue", "tickets_4w"])
        for n, s in sorted(sc.current.items(), key=lambda kv: kv[1].score):
            d = sc.ds.devices[n]
            ch = "" if n not in prev else f"{s.score - prev[n].score:+.2f}"
            w.writerow([n, d.user, d.department, d.site, d.floor, d.model, "yes" if d.vip else "no", f"{s.score:.2f}",
                        band(s.score, sc.cfg["bands"]), *[s.categories.get(c, "") for c in CATEGORIES], ch,
                        s.capped, issue_text(s, sc.cfg), tickets.get(n, 0)])


def write_top_fixes(fixes: list[ins.Fix], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "issue", "kind", "scope", "devices", "fleet_gain", "site_gain", "gain_per_device",
                    "trend", "next_step"])
        for i, f in enumerate(fixes, 1):
            w.writerow([i, f.issue, f.kind, f.scope, f.devices, f"{f.fleet_gain:.2f}",
                        "" if f.scope_gain is None else f"{f.scope_gain:.2f}", f"{f.device_gain:.2f}", f.trend, f.action])


# review.md ------------------------------------------------------------------------------------------------------
def review_md(sc: Scores, fixes: list[ins.Fix], title: str) -> str:
    sm = summary(sc, fixes)
    cfg = sc.cfg
    cats, prev_cats = sc.category_means(), sc.category_means(-2) if len(sc.weeks) > 1 else {}
    lines = [f"# Weekly DEX review · {title} · {ins.window_label(sc)}", "",
             f"Fleet score **{fmt(sm['fleet'])}** / 10 ({fmt(sm['change'], True)} vs the previous week) · "
             f"{sm['scored']} of {sm['devices']} devices scored · **{sm['below6']} below 6**.", ""]
    trend = " → ".join(fmt(v) for v in sc.trend())
    lines += [f"Trend over {len(sc.weeks)} weeks: {trend}.", "", "| Category | Score | Change |", "|---|---|---|"]
    for c in CATEGORIES:
        if c in cats:
            ch = None if c not in prev_cats else cats[c] - prev_cats[c]
            lines.append(f"| {cfg['categories'][c]['label']} | {cats[c]:.1f} | {fmt(ch, True)} |")
    lines += ["", "## Top 5 fixes", ""]
    if fixes:
        lines.append(f"Doing all five would take the fleet from {fmt(sm['fleet'])} to about "
                     f"**{fmt(sm['after_top5'])}** (what-if re-scoring of this week's telemetry).")
        lines.append("")
        for i, f in enumerate(fixes[:5], 1):
            lines.append(f"{i}. **{f.issue}** · {f.gain_text} · {f.trend}  \n   Next step: {f.action}")
    else:
        lines.append("Nothing stands out this week.")
    lines += ["", "## What changed this week", ""]
    drops = [(c, cats[c] - prev_cats[c]) for c in cats if c in prev_cats and cats[c] - prev_cats[c] <= -0.1]
    for c, d in sorted(drops, key=lambda x: x[1]):
        lines.append(f"- {cfg['categories'][c]['label']} fell {abs(d):.2f}.")
    for attr, word in (("site", "Site"), ("department", "Department")):
        for g in sc.groups(attr):
            if g["change"] is not None and g["change"] <= -0.25:
                lines.append(f"- {word} {g['name']}: {fmt(g['score'])} ({g['change']:+.2f}).")
    new = [f for f in fixes[:5] if f.trend in ("new", "growing")]
    for f in new:
        lines.append(f"- {f.trend.capitalize()}: {f.issue}.")
    for n, before, now in ins.biggest_drops(sc, 5):
        lines.append(f"- {n} dropped from {before:.1f} to {now:.1f} ({issue_text(sc.current[n], cfg) or 'no single cause'}).")
    if lines[-1] == "":
        lines.append("No significant change.")
    lines += ["", "## Sites and departments", "", "| | Devices | Score | Change | Below 6 | Weakest area |",
              "|---|---|---|---|---|---|"]
    for attr in ("site", "department"):
        for g in sc.groups(attr):
            weak = cfg["categories"][g["weakest"]]["label"] if g["weakest"] else ""
            lines.append(f"| {g['name']} | {g['devices']} | {fmt(g['score'])} | {fmt(g['change'], True)} | "
                         f"{g['below6']} | {weak} |")
    vips = ins.vip_below(sc)
    lines += ["", "## VIP devices below 7", ""]
    lines += [f"- **{d.name}** ({d.department}, {d.location}): {s.score:.1f}. {issue_text(s, cfg)}"
              f"{'; capped by ' + s.capped if s.capped else ''}." for d, s in vips] or ["None this week."]
    tc = ins.ticket_correlation(sc)
    lines += ["", "## Score vs tickets", ""]
    if tc["has_tickets"]:
        lines += [f"Tickets opened in the last {tc['weeks']} weeks, by this week's score band. "
                  f"Correlation between score and tickets: r = {tc['pearson']}.", "",
                  "| Score band | Devices | Tickets | Tickets per device |", "|---|---|---|---|"]
        lines += [f"| {r['band']} | {r['devices']} | {r['tickets']} | {fmt(r['per_device']) if r['per_device'] is not None else '–'} |"
                  for r in tc["rows"]]
        lines += ["", f"{tc['below6_silent']} of the {tc['below6']} devices below 6 opened no ticket in "
                  f"{tc['weeks']} weeks: the telemetry sees problems nobody reported."]
    else:
        lines.append("No tickets file given.")
    nod = ins.no_data(sc)
    lines += ["", "## Data quality", "",
              f"- {sm['events']:,} events read; unreadable JSONL lines skipped: {sc.ds.bad_lines}; "
              f"events of unknown types ignored: {sc.ds.unknown_types}.",
              f"- Devices that sent nothing this week: {len(nod)}" + (f" ({', '.join(nod[:10])})" if nod else "") + ".",
              f"- Devices in the events but not in devices.csv: {len(sc.ds.unknown_devices)}."]
    not_collected = [cfg["metrics"][k]["label"] for k, m in cfg["metrics"].items()
                     if m["agg"] in ("per_day", "minutes_per_day") and k not in sc.collected]
    if not_collected:
        lines.append(f"- Not collected by any device, left out of the score: {', '.join(not_collected)}.")
    lines += ["", "Scores come from telemetry; how they are built: docs/scoring.md. Gains are what-if estimates, "
              "not promises. Per-device detail: `device_scores.csv`.", ""]
    return "\n".join(lines)


# HTML -----------------------------------------------------------------------------------------------------------
CSS = """
:root{--bg:#f6f7f8;--card:#fff;--ink:#1b2128;--muted:#66717c;--line:#e2e5e9;--grid:#eef0f2;--ok:#1e7a4c;--warn:#a46200;--bad:#b42318;--info:#0b6bcb;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}
@media (prefers-color-scheme:dark){:root{--bg:#13171b;--card:#1b2127;--ink:#e7eaee;--muted:#9aa5b0;--line:#2b333b;--grid:#232a31;--ok:#4cc38a;--warn:#f0a83a;--bad:#f2685c;--info:#5aa9f0;
--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 18px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 12px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile b{display:block;font-size:28px}
.tile small{display:block;font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}.card h3{font-size:14px;margin:0 0 6px}
.areas{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.area{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:10px;padding:10px 14px}
.area.critical{border-left-color:var(--bad)}.area.warning{border-left-color:var(--warn)}.area.info{border-left-color:var(--info)}
.area span{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.area b{font-size:22px}.area small{display:block;font-size:12px;color:var(--muted);margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
tr:last-child td{border-bottom:0}.wrap{overflow-x:auto}.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;white-space:nowrap;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.critical{color:var(--bad)}.warning{color:var(--warn)}.info{color:var(--info)}.ok{color:var(--ok)}
.area.critical,.area.warning,.area.info,.area.ok{color:inherit}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;min-width:120px}.bar i{display:block;height:100%;background:var(--ok)}
.bar i.info{background:var(--info)}.bar i.warning{background:var(--warn)}.bar i.critical{background:var(--bad)}
td.fix{font-size:13px;color:var(--muted);max-width:380px}footer{margin-top:40px;font-size:13px}
svg{display:block;width:100%;height:auto}svg text{fill:var(--muted);font:11px system-ui,-apple-system,Segoe UI,sans-serif}
svg .gl{stroke:var(--grid);stroke-width:1}svg .ax{stroke:var(--line);stroke-width:1}svg .lbl{fill:var(--ink);font-weight:600}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.spark{width:96px;height:26px}
"""

VIP_PILL = " <span class='pill warning'>VIP</span>"
SERIES = ["var(--s1)", "var(--s2)", "var(--s3)"]
STATUS = {"critical": "var(--bad)", "warning": "var(--warn)", "info": "var(--info)", "ok": "var(--ok)"}


def svg_histogram(scores: list[float]) -> str:
    bins = [0] * 10
    for s in scores:
        bins[min(9, int(s))] += 1
    w, h, pad_l, pad_b, top = 520, 200, 30, 24, 16
    peak = max(bins) or 1
    bw = (w - pad_l - 10) / 10
    out = [f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Devices per score band'>"]
    for i in range(5):
        y = top + (h - pad_b - top) * i / 4
        out.append(f"<line class='gl' x1='{pad_l}' x2='{w - 10}' y1='{y:.1f}' y2='{y:.1f}'/>"
                   f"<text x='{pad_l - 6}' y='{y + 4:.1f}' text-anchor='end'>{round(peak * (4 - i) / 4)}</text>")
    for i, n in enumerate(bins):
        bh = (h - pad_b - top) * n / peak
        x = pad_l + i * bw + 2
        y = h - pad_b - bh
        color = STATUS[cls(i + 0.5)]
        out.append(f"<g><title>Score {i}–{i + 1}: {n} devices</title>"
                   f"<path d='M{x:.1f},{h - pad_b} V{y + min(4, bh):.1f} q0,-4 4,-4 H{x + bw - 8:.1f} q4,0 4,4 "
                   f"V{h - pad_b} Z' fill='{color}'/>" if bh >= 4 else
                   f"<g><title>Score {i}–{i + 1}: {n} devices</title><rect x='{x:.1f}' y='{y:.1f}' width='{bw - 4:.1f}' "
                   f"height='{bh:.1f}' fill='{color}'/>")
        if n:
            out.append(f"<text class='lbl' x='{x + (bw - 4) / 2:.1f}' y='{y - 4:.1f}' text-anchor='middle'>{n}</text>")
        out.append(f"</g><text x='{x + (bw - 4) / 2:.1f}' y='{h - 8}' text-anchor='middle'>{i}–{i + 1}</text>")
    out.append(f"<line class='ax' x1='{pad_l}' x2='{w - 10}' y1='{h - pad_b}' y2='{h - pad_b}'/></svg>")
    return "".join(out)


def svg_trend(labels: list[str], series: list[tuple[str, list[float | None], str, float]]) -> str:
    """Line chart: series = (name, values, color, stroke width). One y axis, auto range around the data."""
    vals = [v for _, vs, _, _ in series for v in vs if v is not None]
    if not vals:
        return ""
    lo, hi = max(0, math.floor(min(vals) - 0.25)), min(10, math.ceil(max(vals) + 0.25))
    hi = hi if hi > lo else lo + 1
    w, h, pad_l, pad_r, pad_b, top = 520, 220, 34, 92, 24, 12
    def X(i):
        return pad_l + (w - pad_l - pad_r) * (i / max(1, len(labels) - 1))
    def Y(v):
        return top + (h - pad_b - top) * (1 - (v - lo) / (hi - lo))
    out = [f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Weekly score trend'>"]
    steps = int((hi - lo) * 2) if hi - lo <= 2 else int(hi - lo)
    for i in range(steps + 1):
        v = lo + (hi - lo) * i / steps
        out.append(f"<line class='gl' x1='{pad_l}' x2='{w - pad_r}' y1='{Y(v):.1f}' y2='{Y(v):.1f}'/>"
                   f"<text x='{pad_l - 6}' y='{Y(v) + 4:.1f}' text-anchor='end'>{v:.1f}</text>")
    for i, lab in enumerate(labels):
        out.append(f"<text x='{X(i):.1f}' y='{h - 6}' text-anchor='middle'>{e(lab)}</text>")
    ends = []
    for name, vs, color, sw in series:
        pts = [(X(i), Y(v), v, i) for i, v in enumerate(vs) if v is not None]
        if not pts:
            continue
        out.append(f"<polyline fill='none' stroke='{color}' stroke-width='{sw}' stroke-linejoin='round' "
                   f"stroke-linecap='round' points='{' '.join(f'{x:.1f},{y:.1f}' for x, y, _, _ in pts)}'/>")
        for x, y, v, i in pts:
            out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{4 if sw > 2 else 3}' fill='{color}' "
                       f"stroke='var(--card)' stroke-width='2'><title>{e(name)} · {e(labels[i])}: {v:.2f}</title></circle>")
        ends.append([pts[-1][1], name, pts[-1][2]])
    ends.sort()
    for i in range(1, len(ends)):                    # keep end labels from overlapping
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 13)
    for y, name, v in ends:
        out.append(f"<text class='lbl' x='{w - pad_r + 8}' y='{y + 4:.1f}'>{e(name)} {v:.1f}</text>")
    out.append("</svg>")
    return "".join(out)


def svg_spark(vals: list[float | None]) -> str:
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    hi = hi if hi > lo else lo + 1
    w, h = 96, 26
    xy = [(4 + (w - 8) * i / (len(vals) - 1), 3 + (h - 6) * (1 - (v - lo) / (hi - lo))) for i, v in pts]
    last = xy[-1]
    return (f"<svg class='spark' viewBox='0 0 {w} {h}' aria-hidden='true'><polyline fill='none' stroke='var(--muted)' "
            f"stroke-width='1.5' points='{' '.join(f'{x:.1f},{y:.1f}' for x, y in xy)}'/>"
            f"<circle cx='{last[0]:.1f}' cy='{last[1]:.1f}' r='2.5' fill='{STATUS[cls(pts[-1][1])]}'/></svg>")


def _change(v: float | None) -> str:
    if v is None:
        return "<span class='muted'>–</span>"
    c = "critical" if v <= -0.25 else "warning" if v < -0.05 else "ok" if v >= 0.05 else "muted"
    return f"<span class='{c}'>{fmt(v, True)}</span>"


def _group_table(sc: Scores, attr: str, title: str) -> str:
    rows = "".join(
        f"<tr><td><b>{e(g['name'])}</b></td><td class='num'>{g['devices']}</td>"
        f"<td class='num'><b class='{cls(g['score'])}'>{fmt(g['score'])}</b></td><td class='num'>{_change(g['change'])}</td>"
        f"<td class='num'>{g['below6'] or '<span class=muted>0</span>'}</td><td>{svg_spark(g['trend'])}</td>"
        f"<td class='muted'>{e(sc.cfg['categories'][g['weakest']]['label']) if g['weakest'] else ''}</td></tr>"
        for g in sc.groups(attr))
    return (f"<div class='wrap'><table><thead><tr><th>{title}</th><th class='num'>Devices</th><th class='num'>Score</th>"
            f"<th class='num'>Change</th><th class='num'>Below 6</th><th>Trend</th><th>Weakest area</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def render_html(sc: Scores, fixes: list[ins.Fix], title: str) -> str:
    sm = summary(sc, fixes)
    cfg = sc.cfg
    tc = ins.ticket_correlation(sc)
    labels = [f"{s:%b %d}".replace(" 0", " ") for s, _ in sc.windows]
    sites = sorted({d.site for d in sc.ds.devices.values() if d.site})
    site_rows = {g["name"]: g["trend"] for g in sc.groups("site")}
    series = [("Fleet", sc.trend(), "var(--ink)", 3)]
    series += [(s, site_rows.get(s, []), SERIES[i], 2) for i, s in enumerate(sites[:3])]
    legend = "".join(f"<span><i style='background:{c};height:{3 if w > 2 else 2}px'></i>{e(n)}</span>"
                     for n, _, c, w in series)
    if len(sites) > 3:
        legend += f"<span>{len(sites) - 3} more sites in the table below</span>"
    tiles = (f"<div class='tile'><span class='muted'>Fleet score</span><b class='{cls(sm['fleet'])}'>{fmt(sm['fleet'])}"
             f"</b><small>out of 10, mean of devices</small></div>"
             f"<div class='tile'><span class='muted'>Devices scored</span><b>{sm['scored']}</b>"
             f"<small>of {sm['devices']} in devices.csv</small></div>"
             f"<div class='tile'><span class='muted'>Devices below 6</span><b class='warning'>{sm['below6']}</b>"
             f"<small>{sm['below6'] * 100 // max(1, sm['scored'])}% of the fleet</small></div>"
             f"<div class='tile'><span class='muted'>Week change</span><b>{_change(sm['change'])}</b>"
             f"<small>vs the previous 7 days</small></div>"
             f"<div class='tile'><span class='muted'>After the top 5 fixes</span><b class='{cls(sm['after_top5'])}'>"
             f"{fmt(sm['after_top5'])}</b><small>what-if estimate</small></div>")
    cats, prev = sc.category_means(), sc.category_means(-2) if len(sc.weeks) > 1 else {}
    drags = ins.category_drags(sc)
    areas = ""
    for c in CATEGORIES:
        if c not in cats:
            continue
        ch = None if c not in prev else cats[c] - prev[c]
        why = ", ".join(f"{cfg['metrics'][k]['label']}" + (f" ({n} devices)" if n else "") for k, n in drags.get(c, []))
        areas += (f"<div class='area {cls(cats[c])}'><span>{e(cfg['categories'][c]['label'])} · weight "
                  f"{cfg['categories'][c]['weight']:.0%}</span><b class='{cls(cats[c])}'>{cats[c]:.1f}</b> {_change(ch)}"
                  f"<div class='bar'><i class='{cls(cats[c])}' style='width:{cats[c] * 10:.0f}%'></i></div>"
                  f"<small>{('Main drag: ' + e(why)) if why else 'Nothing stands out.'}</small></div>")
    peak = max((f.fleet_gain for f in fixes), default=1) or 1
    frows = "".join(
        f"<tr><td class='num'>{i}</td><td><b>{e(f.issue)}</b><br><span class='pill {'warning' if f.trend in ('new', 'growing') else 'info'}'>"
        f"{e(f.trend)}</span></td><td class='num'>{f.devices}</td><td><b>{e(f.gain_text)}</b>"
        f"<div class='bar'><i class='info' style='width:{f.fleet_gain / peak * 100:.0f}%'></i></div>"
        f"<span class='muted'>+{f.device_gain:.1f} per device</span></td><td class='fix'>{e(f.action)}</td></tr>"
        for i, f in enumerate(fixes[:cfg["fixes"]["show"]], 1))
    worst = "".join(
        f"<tr><td><b>{e(d.name)}</b>{VIP_PILL if d.vip else ''}<br>"
        f"<span class='muted'>{e(d.model)}</span></td><td>{e(d.location)}<br><span class='muted'>{e(d.department)}</span></td>"
        f"<td class='num'><b class='{cls(s.score)}'>{s.score:.1f}</b></td><td>{e(issue_text(s, cfg))}</td>"
        f"<td class='muted'>{e(s.capped) or '–'}</td><td class='num'>{tc['per_device'].get(d.name, 0)}</td></tr>"
        for d, s in ins.worst_devices(sc, 15))
    vips = "".join(
        f"<tr><td><b>{e(d.name)}</b><br><span class='muted'>{e(d.model)}</span></td><td>{e(d.department)} · {e(d.location)}</td>"
        f"<td class='num'><b class='{cls(s.score)}'>{s.score:.1f}</b></td><td>{e(issue_text(s, cfg))}</td>"
        f"<td>{', '.join(e(describe(k, s.metrics[k], cfg)) for k in [k for k in sorted(s.losses, key=s.losses.get, reverse=True) if k not in (s.main_issue, 'sentiment')][:2])}</td></tr>"
        for d, s in ins.vip_below(sc))
    peak_t = max((r["per_device"] or 0 for r in tc["rows"]), default=1) or 1
    trows = "".join(
        f"<tr><td><b>{e(r['band'])}</b></td><td class='num'>{r['devices']}</td><td class='num'>{r['tickets']}</td>"
        f"<td class='num'>{fmt(r['per_device']) if r['per_device'] is not None else '–'}</td>"
        f"<td><div class='bar'><i class='warning' style='width:{(r['per_device'] or 0) / peak_t * 100:.0f}%'></i></div></td></tr>"
        for r in tc["rows"])
    ticket_note = (f"Tickets opened in the last {tc['weeks']} weeks, by this week's score band. Pearson r between score "
                   f"and tickets: {tc['pearson']}. <b>{tc['below6_silent']} of the {tc['below6']} devices below 6 opened "
                   f"no ticket</b>: the telemetry sees problems nobody reported.") if tc["has_tickets"] else \
        "No tickets file given: add an export from your ITSM tool to compare scores with tickets."
    quality = (f"{sm['events']:,} events · unreadable lines skipped: {sc.ds.bad_lines} · "
               f"devices without data this week: {len(ins.no_data(sc))}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head><body><main>
<h1>{e(title)}</h1><p class="muted">Week {ins.window_label(sc)} · {sm['devices']} devices · {len(sites)} sites ·
{len({d.department for d in sc.ds.devices.values()})} departments · {quality}</p>
<div class="tiles">{tiles}</div>
<div class="grid2" style="margin-top:12px">
<div class="card"><h3>Score distribution · this week</h3>{svg_histogram([s.score for s in sc.current.values()])}</div>
<div class="card"><h3>Trend · {len(sc.weeks)} weeks</h3>{svg_trend(labels, series)}<div class="legend">{legend}</div></div>
</div>
<h2>What drags the score down</h2><div class="areas">{areas}</div>
<h2>Top fixes · which problems, if fixed, raise the score most</h2><div class="wrap"><table><thead><tr><th class="num">#</th>
<th>Problem</th><th class="num">Devices</th><th>Estimated gain</th><th>Next step</th></tr></thead>
<tbody>{frows or "<tr><td colspan=5>Nothing stands out this week.</td></tr>"}</tbody></table></div>
<h2>Sites</h2>{_group_table(sc, "site", "Site")}
<h2>Departments</h2>{_group_table(sc, "department", "Department")}
<h2>Worst devices</h2><div class="wrap"><table><thead><tr><th>Device</th><th>Where</th><th class="num">Score</th>
<th>Main issue</th><th>Capped by</th><th class="num">Tickets 4 wk</th></tr></thead><tbody>{worst}</tbody></table></div>
<h2>VIP devices below 7</h2><div class="wrap"><table><thead><tr><th>Device</th><th>Who / where</th><th class="num">Score</th>
<th>Main issue</th><th>Also</th></tr></thead><tbody>{vips or "<tr><td colspan=5>No VIP device below 7.</td></tr>"}</tbody></table></div>
<h2>Score vs tickets</h2><p class="muted">{ticket_note}</p><div class="wrap"><table><thead><tr><th>Score band</th>
<th class="num">Devices</th><th class="num">Tickets</th><th class="num">Per device</th><th></th></tr></thead><tbody>{trows}</tbody></table></div>
<footer class="muted">A lab to learn how digital experience scoring works; not affiliated with any DEX product. Scores come from
telemetry only. Model, thresholds and weights: docs/scoring.md. Gains are what-if estimates, not promises.</footer>
</main></body></html>"""
