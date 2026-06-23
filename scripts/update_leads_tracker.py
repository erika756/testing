#!/usr/bin/env python3
"""
Weekly Newsletter Leads Tracker
Fetches the 3 most recent Asana newsletter/leads projects, cross-references
companies against company_master.json (excluding Clients), then rewrites
the Notion page with fresh stats.

Required env vars:
  ASANA_TOKEN       - Asana personal access token
  NOTION_TOKEN      - Notion integration secret
  NOTION_PAGE_ID    - ID of the Notion page to update (no dashes)
"""

import os
import re
import json
import datetime
import requests
from collections import defaultdict

ASANA_TOKEN = os.environ["ASANA_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_PAGE_ID = os.environ.get("NOTION_PAGE_ID", "3887b561cbb68198a8e4f02a52f531af")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANY_MASTER_PATH = os.path.join(SCRIPT_DIR, "company_master.json")

ASANA_BASE = "https://app.asana.com/api/1.0"
NOTION_BASE = "https://api.notion.com/v1"

ASANA_HEADERS = {"Authorization": f"Bearer {ASANA_TOKEN}", "Accept": "application/json"}
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Names that should be excluded (placeholder/instruction tasks)
SKIP_TASK_PATTERNS = re.compile(
    r"share with|ag\.assistant|@gmail\.com|interest form|client name|^\s*$",
    re.IGNORECASE,
)

CANDIDATE_SECTION_RE = re.compile(r"CANDIDATE\s+\d+\s*[-–]\s*([^-–]+)[-–]\s*([^-–]+)[-–]", re.IGNORECASE)


def load_company_master():
    with open(COMPANY_MASTER_PATH) as f:
        return json.load(f)


# ── Asana helpers ─────────────────────────────────────────────────────────────

def asana_get(path, params=None):
    r = requests.get(f"{ASANA_BASE}{path}", headers=ASANA_HEADERS, params=params or {})
    r.raise_for_status()
    return r.json()["data"]


def find_newsletter_projects(limit=3):
    """Search for the most recent newsletter/leads projects and return up to `limit`."""
    workspaces = asana_get("/workspaces")
    workspace_gid = workspaces[0]["gid"]

    results = asana_get(
        "/projects",
        params={"workspace": workspace_gid, "limit": 100, "opt_fields": "name,created_at"},
    )

    newsletter_re = re.compile(
        r"(newsletter|news\s*\[leads\])", re.IGNORECASE
    )
    matches = [p for p in results if newsletter_re.search(p["name"])]
    matches.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return matches[:limit]


def get_project_tasks(project_gid):
    tasks = asana_get(
        "/tasks",
        params={
            "project": project_gid,
            "limit": 100,
            "opt_fields": "name,memberships.section.name,custom_fields,completed",
        },
    )
    return tasks


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_candidate_section(section_name):
    """Return (candidate_name, role) from section name, or None if not a candidate section."""
    m = CANDIDATE_SECTION_RE.match(section_name)
    if not m:
        return None
    candidate = m.group(1).strip().title()
    role = m.group(2).strip().title()
    return candidate, role


def cf_value(task, field_name):
    for cf in task.get("custom_fields") or []:
        if cf.get("name") == field_name:
            ev = cf.get("enum_value")
            if ev:
                return ev.get("name", "")
            return cf.get("display_value") or cf.get("text_value") or ""
    return ""


def normalize_company(name):
    return name.strip().upper().replace("​", "").replace("‌", "").replace("‍", "").strip()


def label_week(project_name, created_at):
    """Derive a short week label from project name or creation date."""
    date_re = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", project_name)
    if date_re:
        return f"{date_re.group(1)} {date_re.group(2)[:3]} {date_re.group(3)}"
    if created_at:
        d = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return d.strftime("%-d %b %Y")
    return project_name


# ── Core analysis ─────────────────────────────────────────────────────────────

def build_report(projects, company_master):
    """Returns structured report data across the given projects."""
    rows = []  # {week, candidate, role, company, excel_type, status}

    for proj in projects:
        week = label_week(proj["name"], proj.get("created_at", ""))
        tasks = get_project_tasks(proj["gid"])

        for task in tasks:
            if not task.get("memberships"):
                continue
            section_name = task["memberships"][0].get("section", {}).get("name", "")
            parsed = parse_candidate_section(section_name)
            if not parsed:
                continue
            candidate, role = parsed

            company_raw = task.get("name", "").strip()
            if SKIP_TASK_PATTERNS.search(company_raw):
                continue

            company_norm = normalize_company(company_raw)
            excel_type = company_master.get(company_norm)
            if excel_type is None:
                continue  # not in master sheet
            if excel_type == "Client":
                continue  # exclude clients

            status = cf_value(task, "Status") or cf_value(task, "status") or ""
            rows.append({
                "week": week,
                "candidate": candidate,
                "role": role,
                "company": company_norm,
                "excel_type": excel_type,
                "status": status,
            })

    return rows


def compute_stats(rows):
    weekly = defaultdict(lambda: {"candidates": set(), "companies": set(), "selected": 0, "total": 0})
    company_interest = defaultdict(lambda: {"candidates": set(), "selected": False, "type": ""})
    candidate_data = defaultdict(lambda: {"role": "", "companies": []})

    for r in rows:
        w = r["week"]
        weekly[w]["candidates"].add(r["candidate"])
        weekly[w]["companies"].add(r["company"])
        weekly[w]["total"] += 1
        is_selected = "selected" in r["status"].lower() and "not" not in r["status"].lower()
        is_positive = is_selected or "interested" in r["status"].lower() or "interviewing" in r["status"].lower()
        if is_positive:
            weekly[w]["selected"] += 1

        company_interest[r["company"]]["candidates"].add(r["candidate"])
        company_interest[r["company"]]["type"] = r["excel_type"]
        if is_positive:
            company_interest[r["company"]]["selected"] = True

        candidate_data[r["candidate"]]["role"] = r["role"]
        candidate_data[r["candidate"]]["companies"].append(r)

    return weekly, company_interest, candidate_data


# ── Notion content builder ────────────────────────────────────────────────────

def status_emoji(status):
    if not status or status == "Not set":
        return "—"
    s = status.lower()
    if "not selected" in s:
        return "❌ Not Selected"
    if "selected" in s:
        return "✅ Selected"
    if "interviewing" in s:
        return "🔄 Interviewing"
    if "placed" in s:
        return "🏆 Placed"
    if "lost" in s or "failed" in s:
        return "❌ Lost"
    if "interested" in s:
        return "✅ Candidate interested"
    return status


def build_notion_blocks(rows, weekly, company_interest, candidate_data, today):
    blocks = []

    def paragraph(text):
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def heading(text, level=2):
        t = f"heading_{level}"
        return {"object": "block", "type": t, t: {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    def table_block(rows_data, header=True):
        width = len(rows_data[0])
        cells = []
        for i, row in enumerate(rows_data):
            cell_row = []
            for cell in row:
                cell_row.append([{"type": "text", "text": {"content": str(cell)},
                                   "annotations": {"bold": (i == 0 and header)}}])
            cells.append({"cells": cell_row})
        return {
            "object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": header, "has_row_header": False, "children": cells}
        }

    def callout(text, emoji="ℹ️"):
        return {"object": "block", "type": "callout",
                "callout": {"rich_text": [{"type": "text", "text": {"content": text}}],
                             "icon": {"type": "emoji", "emoji": emoji}}}

    blocks.append(paragraph(f"Last updated: {today}  ·  Auto-updated every Monday via GitHub Action"))
    blocks.append(callout(
        "Only companies in the Client & Lead Master sheet with type Lead Type A or B are included. Clients are excluded.",
        "📋"
    ))
    blocks.append(divider())

    # Weekly trend
    blocks.append(heading("📈 Weekly Trend"))
    week_rows = [["Week", "Candidates", "Unique Companies", "Positive Responses", "Rate"]]
    for wk in sorted(weekly.keys()):
        s = weekly[wk]
        total = s["total"]
        rate = f"{round(s['selected']/total*100)}%" if total else "—"
        week_rows.append([wk, len(s["candidates"]), len(s["companies"]), s["selected"], rate])
    blocks.append(table_block(week_rows))
    blocks.append(divider())

    # Company interest ranking
    blocks.append(heading("🏆 Companies by Total Candidate Interest"))
    ranked = sorted(company_interest.items(), key=lambda x: -len(x[1]["candidates"]))
    comp_rows = [["#", "Company", "Type", "# Candidates", "Candidates", "Ever Selected?"]]
    for i, (comp, data) in enumerate(ranked, 1):
        cands = ", ".join(sorted(data["candidates"]))
        selected = "✅ Yes" if data["selected"] else "❌ No"
        comp_rows.append([i, comp.title(), data["type"], len(data["candidates"]), cands, selected])
    blocks.append(table_block(comp_rows))
    blocks.append(divider())

    # Roles
    blocks.append(heading("🎯 Roles by Companies Generated"))
    role_companies = defaultdict(set)
    role_candidate = {}
    for r in rows:
        role_companies[r["role"]].add(r["company"])
        role_candidate[r["role"]] = r["candidate"]
    role_rows = [["Role", "Candidate", "# Companies Shown"]]
    for role, comps in sorted(role_companies.items(), key=lambda x: -len(x[1])):
        role_rows.append([role, role_candidate[role], len(comps)])
    blocks.append(table_block(role_rows))
    blocks.append(divider())

    # Per-candidate detail
    blocks.append(heading("👤 Candidate Detail"))
    for cand, data in sorted(candidate_data.items()):
        # Find the week for this candidate
        weeks = list({r["week"] for r in data["companies"]})
        week_str = " / ".join(sorted(weeks))
        blocks.append(heading(f"{cand} — {data['role']}  ({week_str})", level=3))
        cand_rows = [["Company", "Type", "Status"]]
        for r in data["companies"]:
            cand_rows.append([r["company"].title(), r["excel_type"], status_emoji(r["status"])])
        blocks.append(table_block(cand_rows))

    return blocks


# ── Notion API ────────────────────────────────────────────────────────────────

def clear_notion_page(page_id):
    """Delete all existing blocks from the page."""
    url = f"{NOTION_BASE}/blocks/{page_id}/children"
    while True:
        r = requests.get(url, headers=NOTION_HEADERS)
        r.raise_for_status()
        data = r.json()
        for block in data.get("results", []):
            requests.delete(f"{NOTION_BASE}/blocks/{block['id']}", headers=NOTION_HEADERS)
        if not data.get("has_more"):
            break


def append_notion_blocks(page_id, blocks):
    """Append blocks to a Notion page in chunks of 100."""
    url = f"{NOTION_BASE}/blocks/{page_id}/children"
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        r = requests.patch(url, headers=NOTION_HEADERS, json={"children": chunk})
        if not r.ok:
            print(f"Notion error: {r.status_code} {r.text}")
            r.raise_for_status()


def update_notion_page_title(page_id, today):
    url = f"{NOTION_BASE}/pages/{page_id}"
    payload = {
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": f"📊 Weekly Newsletter Leads — Candidate Interest Tracker"}}]}
        }
    }
    requests.patch(url, headers=NOTION_HEADERS, json=payload)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().strftime("%-d %B %Y")
    print(f"Running leads tracker — {today}")

    company_master = load_company_master()
    print(f"Loaded {len(company_master)} companies from master")

    projects = find_newsletter_projects(limit=3)
    if not projects:
        print("No newsletter projects found — exiting")
        return
    print(f"Found projects: {[p['name'] for p in projects]}")

    rows = build_report(projects, company_master)
    print(f"Filtered to {len(rows)} qualifying company entries")

    if not rows:
        print("No data after filtering — nothing to write")
        return

    weekly, company_interest, candidate_data = compute_stats(rows)
    blocks = build_notion_blocks(rows, weekly, company_interest, candidate_data, today)

    print(f"Clearing Notion page {NOTION_PAGE_ID}…")
    clear_notion_page(NOTION_PAGE_ID)

    print(f"Writing {len(blocks)} blocks…")
    append_notion_blocks(NOTION_PAGE_ID, blocks)

    update_notion_page_title(NOTION_PAGE_ID, today)
    print("Done ✅")


if __name__ == "__main__":
    main()
