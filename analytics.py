#!/usr/bin/env python3
"""
ORSVAI Project Development Accountability — analytics pipeline.

Pulls tasks from the Apps Script web app (or a local data/tasks.json),
computes ORSVAI scores, department progress, and a risk register, and
writes data/analytics.json for the dashboard / board pack.

Run locally:   python analytics.py
In CI:         scheduled via .github/workflows/analytics.yml
Env var:       ORSVAI_SCRIPT_URL  (the Apps Script /exec URL, optional)
"""

import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

STATUS_SCORE = {"Completed": 100, "In Progress": 75, "Delayed": 50, "Not Started": 0}
UNVERIFIED_DISCOUNT = 0.85  # mirrors the frontend scoring engine


def load_tasks():
    """Prefer live Apps Script data; fall back to a committed snapshot."""
    url = os.environ.get("ORSVAI_SCRIPT_URL", "").strip()
    if url:
        try:
            with urllib.request.urlopen(f"{url}?action=list", timeout=30) as r:
                payload = json.loads(r.read().decode())
            tasks = payload.get("tasks", [])
            (DATA_DIR / "tasks.json").write_text(json.dumps(tasks, indent=2))
            print(f"Fetched {len(tasks)} tasks from Apps Script.")
            return tasks
        except Exception as exc:  # noqa: BLE001
            print(f"Live fetch failed ({exc}); using local snapshot.", file=sys.stderr)
    snap = DATA_DIR / "tasks.json"
    if snap.exists():
        return json.loads(snap.read_text())
    print("No data source available — writing empty analytics.")
    return []


def truthy(v):
    return v in (True, "TRUE", "true", "True", 1, "1")


def task_score(t):
    base = STATUS_SCORE.get(t.get("status", "Not Started"), 0)
    if not truthy(t.get("approved")) and base > 0:
        base = round(base * UNVERIFIED_DISCOUNT)
    return base


def is_overdue(t):
    due = t.get("due")
    if not due or t.get("status") == "Completed":
        return False
    try:
        return datetime.fromisoformat(str(due)[:10]).date() < date.today()
    except ValueError:
        return False


def days_since(d):
    try:
        return (date.today() - datetime.fromisoformat(str(d)[:10]).date()).days
    except (ValueError, TypeError):
        return None


def split_archive(tasks):
    """Auto-archive: Completed tasks whose completion is 365+ days old."""
    active, archived = [], []
    for t in tasks:
        ds = days_since(t.get("completedDate"))
        if t.get("archived") or (t.get("status") == "Completed" and ds is not None and ds >= 365):
            t["archived"] = True
            archived.append(t)
        else:
            active.append(t)
    return active, archived


def update_snapshots(active):
    """Append today's portfolio completion and bucket into periods."""
    snap_file = DATA_DIR / "snapshots.json"
    snaps = {}
    if snap_file.exists():
        snaps = json.loads(snap_file.read_text())
    if active:
        pct = round(sum(task_score(t) for t in active) / len(active))
        snaps[date.today().isoformat()] = pct
    snap_file.write_text(json.dumps(snaps, indent=2))

    def bucket(gran):
        out = {}
        for ds in sorted(snaps):
            dt = datetime.fromisoformat(ds).date()
            if gran == "yearly":
                key = f"{dt.year}"
            elif gran == "quarterly":
                key = f"{dt.year} Q{(dt.month - 1) // 3 + 1}"
            elif gran == "monthly":
                key = dt.strftime("%b %y")
            else:  # weekly
                key = f"W{dt.isocalendar().week} '{str(dt.year)[2:]}"
            out[key] = snaps[ds]  # last value in the period wins
        cap = {"weekly": 12, "monthly": 12, "quarterly": 8, "yearly": 5}[gran]
        items = list(out.items())[-cap:]
        return {"labels": [k for k, _ in items], "values": [v for _, v in items]}

    return {g: bucket(g) for g in ("weekly", "monthly", "quarterly", "yearly")}


def leaderboard(active):
    pts = {}
    for t in active:
        d = t.get("dept", "Unassigned")
        m = pts.setdefault(d, {"dept": d, "points": 0, "delivered": 0, "tasks": 0})
        m["tasks"] += 1
        if truthy(t.get("approved")):
            m["points"] += 10
        if t.get("status") == "Completed":
            m["points"] += 25
            m["delivered"] += 1
            due, done = t.get("due"), t.get("completedDate")
            if due and done and str(done)[:10] <= str(due)[:10]:
                m["points"] += 15  # on-time bonus
        elif t.get("status") == "In Progress":
            m["points"] += 5
    return sorted(pts.values(), key=lambda x: -x["points"])


def department_stats(tasks):
    by = {}
    for t in tasks:
        d = t.get("dept", "Unassigned")
        m = by.setdefault(d, {"dept": d, "count": 0, "sum": 0,
                              "Completed": 0, "In Progress": 0,
                              "Delayed": 0, "Not Started": 0})
        m["count"] += 1
        m["sum"] += task_score(t)
        m[t.get("status", "Not Started")] = m.get(t.get("status", "Not Started"), 0) + 1
    out = []
    for m in by.values():
        m["pct"] = round(m["sum"] / m["count"]) if m["count"] else 0
        m["verdict"] = ("On track" if m["pct"] >= 80 else
                        "Watch" if m["pct"] >= 50 else "At risk")
        out.append(m)
    return sorted(out, key=lambda x: -x["pct"])


def risk_register(tasks):
    risks = []
    for t in tasks:
        label = f'{t.get("title", "Untitled")} — {t.get("dept", "?")}'
        if is_overdue(t):
            risks.append({"sev": "high", "tag": "Overdue", "title": label,
                          "desc": f'Past due ({t.get("due")}) and still {t.get("status")}.'})
        if t.get("status") == "Delayed":
            risks.append({"sev": "high", "tag": "Delayed", "title": label,
                          "desc": "Flagged delayed (50%). Verify cause and set a recovery date."})
        if not t.get("O"):
            risks.append({"sev": "high", "tag": "No Owner", "title": label,
                          "desc": "No task without an Owner — assign accountability."})
        if t.get("status") == "Completed" and not truthy(t.get("approved")):
            risks.append({"sev": "med", "tag": "Unverified", "title": label,
                          "desc": "Reported complete but not approved; verification sets actual score."})
    order = {"high": 0, "med": 1, "low": 2}
    return sorted(risks, key=lambda r: order[r["sev"]])


def main():
    all_tasks = load_tasks()
    tasks, archived = split_archive(all_tasks)
    stats = department_stats(tasks)
    risks = risk_register(tasks)
    periods = update_snapshots(tasks)
    board = leaderboard(tasks)
    total = len(tasks)
    overall = round(sum(task_score(t) for t in tasks) / total) if total else 0

    analytics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "tasks": total,
            "departments": len(stats),
            "completion_pct": overall,
            "completed": sum(1 for t in tasks if t.get("status") == "Completed"),
            "in_progress": sum(1 for t in tasks if t.get("status") == "In Progress"),
            "delayed": sum(1 for t in tasks if t.get("status") == "Delayed"),
            "not_started": sum(1 for t in tasks if t.get("status") == "Not Started"),
            "approved": sum(1 for t in tasks if truthy(t.get("approved"))),
            "high_risks": sum(1 for r in risks if r["sev"] == "high"),
            "archived": len(archived),
            "points": sum(d["points"] for d in board),
        },
        "departments": stats,
        "risks": risks,
        "periods": periods,
        "leaderboard": board,
        "archive": [{"id": t.get("id"), "title": t.get("title"),
                     "dept": t.get("dept"), "completedDate": t.get("completedDate")}
                    for t in archived],
    }

    out = DATA_DIR / "analytics.json"
    out.write_text(json.dumps(analytics, indent=2))
    print(f"Wrote {out} — {total} active tasks, {overall}% completion, "
          f"{analytics['totals']['high_risks']} high risks, "
          f"{len(archived)} archived.")


if __name__ == "__main__":
    main()
