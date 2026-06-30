#!/usr/bin/env python3
"""
Weekly Newsletter Leads Tracker
Fetches the 3 most recent Asana newsletter/leads projects, cross-references
companies against company_master.json (excluding Clients), then writes
leads-tracker.html to the repo root.

Required env vars:
  ASANA_TOKEN  - Asana personal access token
"""

import os, re, json, datetime, requests
from collections import defaultdict
from pathlib import Path

ASANA_TOKEN  = os.environ["ASANA_TOKEN"]
ASANA_BASE   = "https://app.asana.com/api/1.0"
ASANA_HEADS  = {"Authorization": f"Bearer {ASANA_TOKEN}", "Accept": "application/json"}

REPO_ROOT    = Path(__file__).parent.parent
MASTER_PATH  = Path(__file__).parent / "company_master.json"
OUTPUT_HTML  = REPO_ROOT / "leads-tracker.html"

SKIP_RE      = re.compile(r"share with|ag\.assistant|@gmail|interest form|client name|^\s*$", re.I)
SECTION_RE   = re.compile(r"CANDIDATE\s*\d+\s*[-–]\s*([^-–]+?)\s*[-–]\s*([^-–]+?)\s*[-–]", re.I)

# ── Asana ─────────────────────────────────────────────────────────────────────

def asana(path, params=None):
    r = requests.get(f"{ASANA_BASE}{path}", headers=ASANA_HEADS, params=params or {})
    r.raise_for_status()
    return r.json()["data"]

def latest_newsletter_projects(n=3):
    ws  = asana("/workspaces")[0]["gid"]
    all_projects = asana("/projects", {"workspace": ws, "limit": 100, "opt_fields": "name,created_at"})

    # Priority 1 — explicitly named leads projects (new format, from W3 onwards)
    leads_pat = re.compile(r"\[leads\]", re.I)
    # Priority 2 — old mixed "Newsletter" projects created before the split
    #              We cap these at 8 weeks ago so future client newsletters are never touched
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(weeks=8)).isoformat()
    newsletter_pat = re.compile(r"newsletter", re.I)

    hits = []
    for p in all_projects:
        name = p.get("name", "")
        created = p.get("created_at", "")
        if leads_pat.search(name):
            hits.append(p)                          # always include explicit leads projects
        elif newsletter_pat.search(name) and created < cutoff:
            hits.append(p)                          # only old newsletters (pre-split)

    hits.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return hits[:n]

def project_tasks(gid):
    return asana("/tasks", {"project": gid, "limit": 100,
                             "opt_fields": "name,memberships.section.name,custom_fields"})

def cf(task, field):
    for f in task.get("custom_fields") or []:
        if f.get("name") == field:
            ev = f.get("enum_value")
            return ev["name"] if ev else (f.get("display_value") or "")
    return ""

# ── Parsing ───────────────────────────────────────────────────────────────────

def norm(name):
    return re.sub(r"[​‌‍]", "", name).strip().upper()

def week_label(proj):
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", proj["name"])
    if m:
        return f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}"
    d = proj.get("created_at", "")
    if d:
        return datetime.datetime.fromisoformat(d.replace("Z","+00:00")).strftime("%-d %b %Y")
    return proj["name"]

def parse_section(sec):
    m = SECTION_RE.match(sec)
    if not m:
        return None
    return m.group(1).strip().title(), m.group(2).strip().title()

# ── Build data ────────────────────────────────────────────────────────────────

def build_rows(projects, master):
    rows = []
    for proj in projects:
        wk = week_label(proj)
        for task in project_tasks(proj["gid"]):
            sec = (task.get("memberships") or [{}])[0].get("section", {}).get("name", "")
            parsed = parse_section(sec)
            if not parsed:
                continue
            candidate, role = parsed
            company_raw = task.get("name", "").strip()
            if SKIP_RE.search(company_raw):
                continue
            company_key = norm(company_raw)
            etype = master.get(company_key)
            if not etype or etype == "Client":
                continue
            status = cf(task, "Status") or cf(task, "status")
            rows.append(dict(week=wk, candidate=candidate, role=role,
                             company=company_key, etype=etype, status=status))
    return rows

def is_positive(status):
    if not status:
        return False
    s = status.lower().strip()
    return "not selected" not in s

def status_icon(status):
    if not status: return "✗ Not Selected"
    if "not selected" in status.lower(): return "✗ Not Selected"
    return "✓ Selected"

# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(rows, today):
    # aggregate
    weekly    = defaultdict(lambda: {"cands": set(), "cos": set(), "pos": 0, "total": 0})
    co_data   = defaultdict(lambda: {"cands": set(), "selected": False, "etype": "", "sel_count": 0, "total": 0})
    cand_data = defaultdict(lambda: {"role": "", "items": [], "weeks": set()})

    week_order = []
    for r in rows:
        w = r["week"]
        if w not in week_order:
            week_order.append(w)
        weekly[w]["cands"].add(r["candidate"])
        weekly[w]["cos"].add(r["company"])
        weekly[w]["total"] += 1
        if is_positive(r["status"]):
            weekly[w]["pos"] += 1
        co_data[r["company"]]["cands"].add(r["candidate"])
        co_data[r["company"]]["etype"] = r["etype"]
        co_data[r["company"]]["total"] += 1
        if is_positive(r["status"]):
            co_data[r["company"]]["selected"] = True
            co_data[r["company"]]["sel_count"] += 1
        cand_data[r["candidate"]]["role"] = r["role"]
        cand_data[r["candidate"]]["items"].append(r)
        cand_data[r["candidate"]]["weeks"].add(w)

    def sel_rate(d):
        return d["sel_count"] / d["total"] if d["total"] else 0

    ranked_cos = sorted(co_data.items(), key=lambda x: -len(x[1]["cands"]))
    ranked_neglected = sorted(
        co_data.items(),
        key=lambda x: (sel_rate(x[1]), -x[1]["total"])
    )[:5]

    # chart data
    w_labels  = json.dumps(week_order)
    w_counts  = json.dumps([len(weekly[w]["cos"]) for w in week_order])

    # KPIs
    total_cos   = len(co_data)
    total_cands = len(cand_data)
    all_pos     = sum(1 for r in rows if is_positive(r["status"]))
    overall_rate = round(all_pos / len(rows) * 100) if rows else 0
    top_co      = ranked_cos[0][0].title() if ranked_cos else "—"
    top_co_n    = len(ranked_cos[0][1]["cands"]) if ranked_cos else 0

    # neglected companies table
    neglected_rows_html = ""
    for i, (co, d) in enumerate(ranked_neglected, 1):
        rate = round(sel_rate(d) * 100)
        cands_str = ", ".join(sorted(d["cands"]))
        neglected_rows_html += f'<tr><td class="rank">{i}</td><td>{co.title()}</td><td><span class="badge">{d["etype"]}</span></td><td class="center notsel">{rate}%</td><td class="center">{d["total"]}</td><td class="cands-list">{cands_str}</td></tr>\n'
    if not neglected_rows_html:
        neglected_rows_html = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No data yet</td></tr>'

    # candidate cards HTML
    cand_cards = ""
    for cand, data in sorted(cand_data.items()):
        weeks_str = " · ".join(sorted(data["weeks"]))
        rows_html = ""
        for r in data["items"]:
            icon = status_icon(r["status"])
            cls = "sel" if is_positive(r["status"]) else "notsel"
            rows_html += f"""<tr>
              <td>{r['company'].title()}</td>
              <td><span class="badge">{r['etype']}</span></td>
              <td class="{cls}">{icon}</td>
            </tr>"""
        cand_cards += f"""
        <div class="card cand-card">
          <div class="cand-header">
            <div>
              <div class="cand-name">{cand}</div>
              <div class="cand-role">{data['role']}</div>
            </div>
            <div class="cand-week">{weeks_str}</div>
          </div>
          <table class="inner-table">
            <thead><tr><th>Company</th><th>Type</th><th>Status</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>"""

    # company ranking rows
    co_rows_html = ""
    for i, (co, data) in enumerate(ranked_cos, 1):
        cands_str = ", ".join(sorted(data["cands"]))
        sel_cls   = "sel" if data["selected"] else "notsel"
        sel_txt   = "✓ Yes" if data["selected"] else "✗ No"
        co_rows_html += f"""<tr>
          <td class="rank">{i}</td>
          <td>{co.title()}</td>
          <td><span class="badge">{data['etype']}</span></td>
          <td class="center">{len(data['cands'])}</td>
          <td class="cands-list">{cands_str}</td>
          <td class="{sel_cls} center">{sel_txt}</td>
        </tr>"""

    # role stats
    role_cos = defaultdict(set)
    role_cand = {}
    for r in rows:
        role_cos[r["role"]].add(r["company"])
        role_cand[r["role"]] = r["candidate"]
    role_rows_html = ""
    for role, cos in sorted(role_cos.items(), key=lambda x: -len(x[1])):
        role_rows_html += f"""<tr>
          <td>{role}</td>
          <td>{role_cand[role]}</td>
          <td class="center">{len(cos)}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekly Newsletter Leads — Candidate Interest Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #21262d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --accent:  #58a6ff;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #d29922;
    --purple:  #bc8cff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 14px;
    padding: 24px;
  }}
  h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 28px; }}
  h2 {{ font-size: 15px; font-weight: 600; color: var(--muted); text-transform: uppercase;
        letter-spacing: .6px; margin-bottom: 14px; margin-top: 32px; }}

  /* KPI strip */
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 32px; }}
  .kpi {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px 20px;
  }}
  .kpi-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 32px; font-weight: 700; line-height: 1; }}
  .kpi-value.accent {{ color: var(--accent); }}
  .kpi-value.green  {{ color: var(--green); }}
  .kpi-value.yellow {{ color: var(--yellow); }}
  .kpi-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

  /* Charts */
  .charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 8px; }}
  @media(max-width:760px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
  .chart-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px;
  }}
  .chart-title {{ font-size: 13px; font-weight: 600; margin-bottom: 14px; color: var(--muted);
                  text-transform: uppercase; letter-spacing: .5px; }}
  .chart-wrap {{ position: relative; height: 200px; }}

  /* Tables */
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px; margin-bottom: 12px; overflow-x: auto;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
        color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,.02); }}
  .rank {{ font-weight: 700; color: var(--muted); width: 30px; }}
  .center {{ text-align: center; }}
  .cands-list {{ color: var(--muted); font-size: 12px; }}
  .sel     {{ color: var(--green); font-weight: 500; }}
  .notsel  {{ color: var(--red); }}
  .neutral {{ color: var(--yellow); }}
  .badge {{
    background: rgba(88,166,255,.12); color: var(--accent);
    border: 1px solid rgba(88,166,255,.25);
    border-radius: 4px; padding: 2px 7px; font-size: 11px; white-space: nowrap;
  }}

  /* Candidate cards */
  .cands-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }}
  .cand-card {{ padding: 18px 20px; }}
  .cand-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }}
  .cand-name {{ font-size: 16px; font-weight: 600; }}
  .cand-role {{ font-size: 12px; color: var(--accent); margin-top: 3px; }}
  .cand-week {{ font-size: 11px; color: var(--muted); text-align: right; }}
  .inner-table td, .inner-table th {{ padding: 7px 10px; }}

  .updated {{ color: var(--muted); font-size: 12px; margin-top: 40px; text-align: right; }}
</style>
</head>
<body>

<h1>📊 Weekly Newsletter Leads</h1>
<p class="subtitle">Candidate Interest Tracker — non-client companies only &nbsp;·&nbsp; auto-updated every Monday</p>

<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-label">Unique Companies</div>
    <div class="kpi-value accent">{total_cos}</div>
    <div class="kpi-sub">across all weeks</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Candidates</div>
    <div class="kpi-value accent">{total_cands}</div>
    <div class="kpi-sub">active this period</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Overall Selection Rate</div>
    <div class="kpi-value green">{overall_rate}%</div>
    <div class="kpi-sub">{all_pos} of {len(rows)} entries selected</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Highest Interest Company</div>
    <div class="kpi-value yellow" style="font-size:18px;margin-top:4px">{top_co}</div>
    <div class="kpi-sub">{top_co_n} candidates interested</div>
  </div>
</div>

<div class="charts-grid" style="grid-template-columns:1fr">
  <div class="chart-card">
    <div class="chart-title">Number of unique companies interested</div>
    <div class="chart-wrap"><canvas id="coChart"></canvas></div>
  </div>
</div>

<h2>The Most Interest Shown (Rankings)</h2>
<div class="card">
  <table>
    <thead><tr><th>#</th><th>Company</th><th>Type</th><th class="center">Candidates</th><th>Who</th><th class="center">Selected?</th></tr></thead>
    <tbody>{co_rows_html}</tbody>
  </table>
</div>

<h2>🚫 Most Neglected Companies</h2>
<div class="card"><table>
  <thead><tr><th>#</th><th>Company</th><th>Type</th><th class="center">Selection Rate</th><th class="center">Times Shown</th><th>Shown to</th></tr></thead>
  <tbody>{neglected_rows_html}</tbody>
</table></div>

<h2>Roles that generate the most interest</h2>
<div class="card">
  <table>
    <thead><tr><th>Role</th><th>Candidate</th><th class="center"># Companies</th></tr></thead>
    <tbody>{role_rows_html}</tbody>
  </table>
</div>

<h2>Candidates Presented</h2>
<div class="cands-grid">{cand_cards}</div>

<p class="updated">Last updated: {today}</p>

<script>
const labels = {w_labels};
const cfg = {{
  responsive: true, maintainAspectRatio: false,
  plugins: {{ legend: {{ display: false }} }},
  scales: {{
    x: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e' }} }},
    y: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e' }} }}
  }}
}};
new Chart(document.getElementById('coChart').getContext('2d'), {{
  type: 'bar',
  data: {{ labels, datasets: [{{
    data: {w_counts},
    backgroundColor: 'rgba(88,166,255,.6)',
    borderColor: '#58a6ff',
    borderWidth: 1, borderRadius: 4
  }}] }},
  options: cfg
}});
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().strftime("%-d %B %Y")
    print(f"Running leads tracker — {today}")
    master = json.loads(MASTER_PATH.read_text())
    projects = latest_newsletter_projects(3)
    if not projects:
        print("No newsletter projects found"); return
    print(f"Projects: {[p['name'] for p in projects]}")
    rows = build_rows(projects, master)
    print(f"{len(rows)} qualifying entries")
    if not rows:
        print("Nothing to write"); return
    html = generate_html(rows, today)
    OUTPUT_HTML.write_text(html)
    print(f"Written → {OUTPUT_HTML}")

if __name__ == "__main__":
    main()
