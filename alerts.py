#!/usr/bin/env python3
"""Emails you when new insider purchases match your rules. Runs after collector.py on GitHub Actions.

Needs these repository secrets: SMTP_USER (your Gmail address) and SMTP_PASS (a Gmail app password).
Optional: ALERT_TO (defaults to SMTP_USER) and SITE_URL (link to your dashboard).
"""
import json, os, re, smtplib, sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

DATA_FILE = "data/filings.json"
ALERTED_FILE = "data/alerted.json"

# ---- your rules (edit these numbers) ----
OFFICER_MIN_VALUE = 100_000    # alert when a CEO/CFO/President/Chair etc. buys at least this much
CLUSTER_MIN_VALUE = 20_000     # alert when 2+ different insiders buy the same company and this buy is at least this much
CLUSTER_DAYS = 7               # how far back to look for the other insiders
SKIP_NAME = re.compile(r"\b(trust|fund|bdc|etf|income|credit|lending)\b", re.I)  # closed-end funds and similar are noisy
# -----------------------------------------

OFFICER = re.compile(r"chief|ceo|cfo|coo|president|chair|treasurer", re.I)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def when(r):
    try:
        return datetime.fromisoformat(r["filed"])
    except (KeyError, ValueError, TypeError):
        return datetime.now(timezone.utc)


def reasons_for(r, rows):
    out = []
    if SKIP_NAME.search(r.get("company", "")):
        return out
    is_officer = r.get("isOfficer") or OFFICER.search(r.get("title", "") or "")
    if is_officer and not r.get("isTenPercentOwner") and r["value"] >= OFFICER_MIN_VALUE:
        out.append("officer buy")
    if r["value"] >= CLUSTER_MIN_VALUE:
        cutoff = when(r) - timedelta(days=CLUSTER_DAYS)
        others = {x["owner"] for x in rows
                  if x["ticker"] == r["ticker"] and x["owner"] != r["owner"] and when(x) >= cutoff}
        if others:
            out.append(f"cluster ({len(others) + 1} insiders)")
    return out


def money(x):
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x:,.0f}"


def build(matches, site):
    n = len(matches)
    tickers = []
    for r, _ in matches:
        if r["ticker"] not in tickers:
            tickers.append(r["ticker"])
    subject = f"Insider buys: {n} new ({', '.join(tickers[:5])}{'…' if len(tickers) > 5 else ''})"
    lines = []
    for r, why in matches:
        who = r["owner"] + (f", {r['title']}" if r.get("title") else "")
        pct = f", holdings +{r['pctIncrease']}%" if r.get("pctIncrease") is not None else ""
        lines.append(f"{r['ticker']} - {r['company']}\n  {who}\n  {r['shares']:,} shares at ${r['price']:.2f} = {money(r['value'])}{pct}\n"
                     f"  Why: {', '.join(why)}\n  {r.get('url', '')}")
    body = "\n\n".join(lines)
    if site:
        body += f"\n\nDashboard: {site}"
    body += "\n\nSource: SEC Form 4 open-market purchases. Not investment advice."
    return subject, body


def main():
    db = load(DATA_FILE, {"rows": []})
    rows = [] if db.get("sample") else db.get("rows", [])
    first_time = not os.path.exists(ALERTED_FILE)
    alerted = set(load(ALERTED_FILE, []))
    if first_time:
        # First run: remember what is already on the site so you do not get a flood of old alerts.
        alerted = {r["id"] for r in rows}
        save(alerted)
        print("First run: marked", len(alerted), "existing purchases as already seen. No email sent.")
        return 0
    matches = []
    for r in rows:
        if r["id"] in alerted:
            continue
        why = reasons_for(r, rows)
        if why:
            matches.append((r, why))
    newly_seen = {r["id"] for r in rows if r["id"] not in alerted and not reasons_for(r, rows)}
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_PASS", "").strip()
    if not matches:
        alerted |= newly_seen
        save(alerted)
        print("No new alerts.")
        return 0
    if not user or not pw:
        print("SMTP_USER / SMTP_PASS secrets are not set, so no email was sent.")
        return 0
    to = os.environ.get("ALERT_TO", "").strip() or user
    subject, body = build(matches, os.environ.get("SITE_URL", "").strip())
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    except Exception as e:  # leave the matches un-alerted so the next run retries
        print("Email failed:", e)
        return 0
    alerted |= newly_seen | {r["id"] for r, _ in matches}
    save(alerted)
    print("Emailed", len(matches), "alerts to", to)
    return 0


def save(alerted):
    os.makedirs("data", exist_ok=True)
    with open(ALERTED_FILE, "w") as f:
        json.dump(sorted(alerted)[-8000:], f)


if __name__ == "__main__":
    sys.exit(main())
