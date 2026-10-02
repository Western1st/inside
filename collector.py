#!/usr/bin/env python3
"""Collects open-market insider purchases (Form 4, transaction code P) from SEC EDGAR.

Runs on GitHub Actions. Uses only the Python standard library.
Needs the environment variable SEC_USER_AGENT, for example: "My Insider Tracker you@example.com".
The SEC asks every automated user to identify themselves and stay under 10 requests per second.
"""
import json, os, re, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
        "&owner=only&start={start}&count=100&output=atom")
DATA_FILE = "data/filings.json"
SEEN_FILE = "data/seen.json"
KEEP_DAYS = 120          # how long purchases stay on the site
MAX_PAGES = 6            # at most 600 feed entries per run
PAUSE = 0.2              # seconds between requests (about 5 per second)
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(url, ua, tries=4):
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
            time.sleep(PAUSE)
            return body
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) or e.code >= 500:
                time.sleep(2 + attempt * 3)
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 + attempt * 3)
    return None


def feed_entries(page_text):
    """Return [(accession, filing_dir_url, updated_iso)] from one Atom page."""
    out = []
    root = ET.fromstring(page_text)
    for e in root.findall(ATOM + "entry"):
        eid = (e.findtext(ATOM + "id") or "")
        m = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})", eid)
        link = e.find(ATOM + "link")
        href = link.get("href") if link is not None else ""
        if not m or not href:
            continue
        out.append((m.group(1), href.rsplit("/", 1)[0], e.findtext(ATOM + "updated") or ""))
    return out


def _txt(node, path):
    if node is None:
        return ""
    el = node.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text, accession, filed, filing_url):
    """Return a list of purchase rows (zero or one per filing) from a Form 4 XML document."""
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    ticker = _txt(issuer, "issuerTradingSymbol").upper()
    company = _txt(issuer, "issuerName")
    cik = _txt(issuer, "issuerCik")
    owners, titles, is_dir, is_off, is_ten = [], [], False, False, False
    for ro in root.findall("reportingOwner"):
        owners.append(_txt(ro, "reportingOwnerId/rptOwnerName"))
        rel = ro.find("reportingOwnerRelationship")
        flag = lambda tag: _txt(rel, tag).lower() in ("1", "true")
        is_dir |= flag("isDirector"); is_off |= flag("isOfficer"); is_ten |= flag("isTenPercentOwner")
        t = _txt(rel, "officerTitle")
        if t and t not in titles:
            titles.append(t)
    shares = value = 0.0
    last_date, owned_after = "", None
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _txt(tx, "transactionCoding/transactionCode") != "P":
            continue
        if _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") not in ("A", ""):
            continue
        n = _num(_txt(tx, "transactionAmounts/transactionShares/value"))
        p = _num(_txt(tx, "transactionAmounts/transactionPricePerShare/value"))
        if not n or not p or p <= 0:
            continue
        shares += n
        value += n * p
        last_date = max(last_date, _txt(tx, "transactionDate/value")[:10])
        oa = _num(_txt(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
        if oa is not None:
            owned_after = oa
    if shares <= 0 or not ticker:
        return []
    before = (owned_after - shares) if owned_after is not None else None
    pct = round(100 * shares / before, 1) if before and before > 0 else None
    return [{
        "id": accession + "|" + (owners[0] if owners else ""),
        "accession": accession, "filed": filed, "tradeDate": last_date,
        "ticker": ticker, "company": company, "cik": cik,
        "owner": " / ".join(o for o in owners if o), "title": ", ".join(titles),
        "isDirector": is_dir, "isOfficer": is_off, "isTenPercentOwner": is_ten,
        "shares": round(shares), "price": round(value / shares, 4), "value": round(value),
        "ownedAfter": owned_after, "pctIncrease": pct, "url": filing_url,
    }]


def primary_xml_name(index_json_text):
    try:
        items = json.loads(index_json_text)["directory"]["item"]
    except (ValueError, KeyError, TypeError):
        return None
    for it in items:
        name = it.get("name", "")
        if name.lower().endswith(".xml") and not name.lower().startswith(("xsl", "filingsummary")):
            return name
    return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def run(ua, fetcher=fetch, now=None):
    now = now or datetime.now(timezone.utc)
    db = load_json(DATA_FILE, {"rows": []})
    rows = [] if db.get("sample") else db.get("rows", [])
    seen = set(load_json(SEEN_FILE, []))
    by_id = {r["id"]: r for r in rows}
    new_acc, stop = [], False
    for page in range(MAX_PAGES):
        text = fetcher(FEED.format(start=page * 100), ua)
        if not text:
            break
        ents = feed_entries(text)
        if not ents:
            break
        fresh = 0
        for acc, d, upd in ents:
            if acc in seen or any(a[0] == acc for a in new_acc):
                continue
            new_acc.append((acc, d, upd)); fresh += 1
        if fresh == 0:
            break
    added = 0
    for acc, d, upd in new_acc:
        idx = fetcher(d + "/index.json", ua)
        name = primary_xml_name(idx) if idx else None
        if name:
            xml = fetcher(d + "/" + name, ua)
            if xml:
                try:
                    for r in parse_form4(xml, acc, upd, d + "/"):
                        if r["id"] not in by_id:
                            added += 1
                        by_id[r["id"]] = r
                except ET.ParseError:
                    pass
                seen.add(acc)
        # if we could not read the filing, leave it unseen so the next run retries it
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    rows = [r for r in by_id.values() if (r.get("tradeDate") or r.get("filed", "")[:10]) >= cutoff]
    rows.sort(key=lambda r: r.get("filed", ""), reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump({"updated": now.isoformat(timespec="seconds"), "rows": rows}, f, separators=(",", ":"))
    keep_seen = sorted(seen)[-8000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(keep_seen, f)
    print(f"checked {len(new_acc)} new filings, added {added} purchases, {len(rows)} on file")
    return rows


if __name__ == "__main__":
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        sys.exit("Set SEC_USER_AGENT to a name and contact email, e.g. 'My Insider Tracker you@example.com'.")
    run(ua)
