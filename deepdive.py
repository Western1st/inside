#!/usr/bin/env python3
"""Builds a company deep dive from free SEC EDGAR data: profile, financials, major customers,
suppliers (other companies that name it as a major customer), and insider buys and sells.

Usage: python deepdive.py AAPL NVDA ...      (or with no tickers: auto-pick recent big insider buys)
Writes data/company/<TICKER>.json and data/company/index.json. Uses only the Python standard library.
Needs SEC_USER_AGENT, same as collector.py.
"""
import json, os, re, sys, time, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from collector import fetch, load_json, primary_xml_name, _txt, _num

OUT_DIR = "data/company"
INDEX_FILE = OUT_DIR + "/index.json"
AUTO_MIN_VALUE = 100_000   # auto mode: deep dive companies with an insider buy at least this big...
AUTO_DAYS = 7              # ...filed in the last this many days
AUTO_MAX = 12              # at most this many companies per automatic run
REFRESH_DAYS = 30          # auto mode skips companies refreshed more recently than this
MAX_FORM4 = 40             # insider filings to read per company
MAX_SUPPLIER_CHECKS = 20   # 10-Ks of possible suppliers to verify per company

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
LINK = "{http://www.xbrl.org/2003/linkbase}"
XLINK = "{http://www.w3.org/1999/xlink}"

REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"]
FIN_TAGS = {
    "netIncome": ["NetIncomeLoss", "ProfitLoss"],
    "grossProfit": ["GrossProfit"],
    "operatingIncome": ["OperatingIncomeLoss"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "assets": ["Assets"],
    "rnd": ["ResearchAndDevelopmentExpense"],
}
SUFFIXES = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|plc|holdings?|group|"
                      r"llc|lp|l\.p|n\.v|nv|s\.a|sa|ag|se|the|class [a-z]|/[a-z]{2}/?)\b\.?", re.I)


def get_json(url, ua):
    text = fetch(url, ua)
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def short_name(name):
    """'NVIDIA CORP' -> 'Nvidia', 'Apple Inc.' -> 'Apple'. Used to match a company inside other filings."""
    s = SUFFIXES.sub(" ", name.replace(",", " ").replace("&amp;", "&"))
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s.title() if s.isupper() else s


def camel_words(local):
    s = re.sub(r"Member$", "", local)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s.strip()


ANON = re.compile(r"^(customer|client|vendor|supplier|distributor|partner|counterparty|reseller|"
                  r"major customer|significant customer|largest customer|top customer)s?\s*(one|two|three|four|five|"
                  r"[a-z]|\d+)?$|^(customer|client|vendor|supplier)s?\b.*\b(one|two|three|[a-z]|\d+)$|"
                  r"^(one|two|three|four|five|single|largest|other|all other)\b|customers?$", re.I)


def is_named(label):
    """False for anonymised members like 'Customer A' or 'One Customer'."""
    return not ANON.search(label.strip())


# ---------- financial history from the companyfacts API ----------

def annual_series(facts, tags, unit="USD", years=6):
    """Fiscal-year values from 10-K filings, newest last: [(fy_end, value)]. First tag with data wins."""
    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    best = []
    for tag in tags:
        vals = gaap.get(tag, {}).get("units", {}).get(unit, [])
        by_end = {}
        for v in vals:
            if v.get("form") not in ("10-K", "10-K/A") or "end" not in v:
                continue
            if "start" in v:   # flows: keep full-year periods only
                days = (datetime.fromisoformat(v["end"]) - datetime.fromisoformat(v["start"])).days
                if not 350 <= days <= 380:
                    continue
            prev = by_end.get(v["end"])
            if prev is None or v.get("filed", "") > prev.get("filed", ""):
                by_end[v["end"]] = v
        series = [(e, by_end[e]["val"]) for e in sorted(by_end)][-years:]
        if series and (not best or series[-1][0] > best[-1][0]):
            best = series
    return best


def financials(facts):
    rev = annual_series(facts, REVENUE_TAGS)
    out = {"revenue": rev}
    for k, tags in FIN_TAGS.items():
        out[k] = annual_series(facts, tags)
    shares = (facts or {}).get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}) \
        .get("units", {}).get("shares", [])
    if shares:
        latest = max(shares, key=lambda v: (v.get("end", ""), v.get("filed", "")))
        out["sharesOutstanding"] = latest.get("val")
    return out


# ---------- customer / supplier concentration from a 10-K's XBRL instance ----------

def filing_files(cik, accession, ua):
    """Return (folder_url, instance_name, label_name) for one filing."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    idx = get_json(folder + "/index.json", ua)
    try:
        names = [it["name"] for it in idx["directory"]["item"]]
    except (TypeError, KeyError):
        return folder, None, None
    inst = next((n for n in names if n.endswith("_htm.xml")), None)
    if not inst:
        inst = next((n for n in names if n.endswith(".xml") and not re.search(
            r"(_cal|_def|_lab|_pre|filingsummary|^r\d+)\.xml$", n, re.I) and not n.lower().startswith("xsl")), None)
    lab = next((n for n in names if n.endswith("_lab.xml")), None)
    return folder, inst, lab


def member_labels(lab_xml):
    """Map member local name -> human label, from the filing's label linkbase."""
    out = {}
    if not lab_xml:
        return out
    try:
        root = ET.fromstring(lab_xml)
    except ET.ParseError:
        return out
    loc_to_name, arc, labels = {}, {}, {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "loc":
            href = el.get(XLINK + "href", "")
            loc_to_name[el.get(XLINK + "label")] = href.split("#", 1)[-1].split("_", 1)[-1]
        elif tag == "labelArc":
            arc.setdefault(el.get(XLINK + "from"), []).append(el.get(XLINK + "to"))
        elif tag == "label":
            role = el.get(XLINK + "role", "")
            if role.endswith("/label") or el.get(XLINK + "label") not in labels:
                labels[el.get(XLINK + "label")] = (el.text or "").strip()
    for loc, name in loc_to_name.items():
        if name.endswith("Member"):
            for to in arc.get(loc, []):
                if labels.get(to):
                    out[name] = re.sub(r"\s*\[Member\]$", "", labels[to])
                    break
    return out


def concentration(instance_xml, labels=None):
    """Read concentration-risk facts. Returns rows: {name, kind, benchmark, pct, amount, period}."""
    labels = labels or {}
    try:
        root = ET.fromstring(instance_xml)
    except ET.ParseError:
        return []
    ctx = {}
    for c in root.findall(XBRLI + "context"):
        dims = {}
        for m in c.iter(XBRLDI + "explicitMember"):
            dims[m.get("dimension", "").split(":")[-1]] = (m.text or "").strip().split(":")[-1]
        p = c.find(XBRLI + "period")
        end = _txt(p, XBRLI + "endDate") or _txt(p, XBRLI + "instant")
        start = _txt(p, XBRLI + "startDate")
        ctx[c.get("id")] = (dims, start, end)
    found = {}
    for el in root:
        local = el.tag.rsplit("}", 1)[-1]
        if local not in ("ConcentrationRiskPercentage1", "ConcentrationRiskPercentage") and \
                local not in REVENUE_TAGS and local not in ("AccountsReceivableNetCurrent", "CostOfRevenue"):
            continue
        if el.get("contextRef") not in ctx or el.text is None:
            continue
        dims, start, end = ctx[el.get("contextRef")]
        who = dims.get("MajorCustomersAxis") or next(
            (v for k, v in dims.items() if re.search(r"(customer|vendor|supplier|counterpart)", k, re.I)
             and k not in ("ConcentrationRiskByTypeAxis", "ConcentrationRiskByBenchmarkAxis")), None)
        if not who:
            continue
        typ = dims.get("ConcentrationRiskByTypeAxis", "")
        bench = dims.get("ConcentrationRiskByBenchmarkAxis", "")
        kind = "supplier" if re.search(r"supplier|vendor|purchase|cost", typ + bench, re.I) else "customer"
        if re.search(r"accountsreceivable|receivable", bench, re.I):
            benchmark = "receivables"
        elif re.search(r"purchase|cost|supplier|vendor", bench, re.I):
            benchmark = "purchases"
        elif bench == "" or re.search(r"revenue|sales", bench, re.I):
            benchmark = "revenue"
        else:
            benchmark = camel_words(bench).lower()
        name = labels.get(who) or camel_words(who)
        val = _num(el.text)
        if val is None:
            continue
        key = (who, kind, benchmark)
        row = found.setdefault(key, {"name": name, "member": who, "kind": kind, "benchmark": benchmark,
                                     "pct": None, "amount": None, "period": ""})
        if local.startswith("ConcentrationRiskPercentage"):
            pct = val * 100 if val <= 1 else val
            if end >= row["period"]:
                row["pct"], row["period"] = round(pct, 1), end
        elif local in REVENUE_TAGS and benchmark == "revenue" and start:
            if end >= row["period"] or row["amount"] is None:
                row["amount"], row["period"] = val, max(end, row["period"])
    rows = [r for r in found.values() if r["pct"] is not None or r["amount"] is not None]
    rows.sort(key=lambda r: (r["kind"], r["benchmark"] != "revenue", -(r["pct"] or 0), -(r["amount"] or 0)))
    return rows


def latest_10k(subs):
    rec = (subs or {}).get("filings", {}).get("recent", {})
    for i, form in enumerate(rec.get("form", [])):
        if form in ("10-K", "20-F", "40-F"):
            return {"accession": rec["accessionNumber"][i], "filed": rec["filingDate"][i],
                    "period": rec["reportDate"][i], "form": form}
    return None


def filing_concentration(cik, accession, ua):
    folder, inst, lab = filing_files(cik, accession, ua)
    if not inst:
        return [], folder
    xml = fetch(f"{folder}/{inst}", ua)
    labels = member_labels(fetch(f"{folder}/{lab}", ua)) if lab else {}
    return (concentration(xml, labels) if xml else []), folder


# ---------- suppliers: other companies' 10-Ks that name this company as a customer ----------

def find_suppliers(name, own_cik, ua, now):
    """Search 10-K full text for the company's name, then confirm each hit in that filing's XBRL."""
    short = short_name(name)
    if len(short) < 3:
        return [], short
    start = (now - timedelta(days=430)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f'"{short}" "major customer"')
    url = (f"https://efts.sec.gov/LATEST/search-index?q={q}&forms=10-K&dateRange=custom"
           f"&startdt={start}&enddt={now.strftime('%Y-%m-%d')}")
    res = get_json(url, ua) or {}
    hits, seen = [], set()
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        ciks = src.get("ciks") or []
        if not ciks or int(ciks[0]) == int(own_cik) or ciks[0] in seen:
            continue
        seen.add(ciks[0])
        disp = (src.get("display_names") or [""])[0]
        hits.append({"cik": ciks[0], "display": re.sub(r"\s*\(CIK \d+\)", "", disp),
                     "accession": src.get("adsh", ""), "filed": src.get("file_date", "")})
    pat = re.compile(r"\b" + re.escape(short.split()[0] if len(short.split()[0]) >= 4 else short) + r"\b", re.I)
    out = []
    for h in hits[:MAX_SUPPLIER_CHECKS]:
        rows, folder = filing_concentration(h["cik"], h["accession"], ua)
        match = [r for r in rows if r["kind"] == "customer" and (pat.search(r["name"]) or pat.search(r["member"]))]
        m = re.match(r"(.*?)\s*\(([A-Z0-9.\-, ]+)\)\s*$", h["display"])
        sup = {"name": m.group(1) if m else h["display"], "ticker": (m.group(2).split(",")[0].strip() if m else ""),
               "cik": h["cik"], "filed": h["filed"], "url": folder + "/",
               "verified": bool(match), "pct": None, "amount": None, "benchmark": ""}
        if match:
            best = sorted(match, key=lambda r: (r["benchmark"] != "revenue", -(r["pct"] or 0)))[0]
            sup.update(pct=best["pct"], amount=best["amount"], benchmark=best["benchmark"])
        out.append(sup)
    out.sort(key=lambda s: (not s["verified"], -(s["pct"] or 0)))
    return out, short


# ---------- insider activity from recent Form 4s ----------

def parse_form4_all(xml_text):
    """Every open-market buy (P) and sell (S) in one Form 4, summed per owner and code."""
    root = ET.fromstring(xml_text)
    owners, titles, roles = [], [], []
    for ro in root.findall("reportingOwner"):
        owners.append(_txt(ro, "reportingOwnerId/rptOwnerName"))
        rel = ro.find("reportingOwnerRelationship")
        t = _txt(rel, "officerTitle")
        if t:
            titles.append(t)
        if _txt(rel, "isDirector").lower() in ("1", "true"):
            roles.append("Director")
        if _txt(rel, "isTenPercentOwner").lower() in ("1", "true"):
            roles.append("10% owner")
    agg = {}
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        n = _num(_txt(tx, "transactionAmounts/transactionShares/value"))
        p = _num(_txt(tx, "transactionAmounts/transactionPricePerShare/value"))
        if not n or not p or p <= 0:
            continue
        a = agg.setdefault(code, {"shares": 0.0, "value": 0.0, "date": ""})
        a["shares"] += n
        a["value"] += n * p
        a["date"] = max(a["date"], _txt(tx, "transactionDate/value")[:10])
    out = []
    for code, a in agg.items():
        out.append({"type": "buy" if code == "P" else "sell", "owner": " / ".join(o for o in owners if o),
                    "role": ", ".join(titles) or ", ".join(roles), "date": a["date"],
                    "shares": round(a["shares"]), "price": round(a["value"] / a["shares"], 2),
                    "value": round(a["value"])})
    return out


def insider_activity(cik, subs, ua):
    rec = (subs or {}).get("filings", {}).get("recent", {})
    out = []
    picked = [i for i, f in enumerate(rec.get("form", [])) if f == "4"][:MAX_FORM4]
    for i in picked:
        acc = rec["accessionNumber"][i]
        folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
        idx = fetch(folder + "/index.json", ua)
        name = primary_xml_name(idx) if idx else None
        xml = fetch(f"{folder}/{name}", ua) if name else None
        if not xml:
            continue
        try:
            for t in parse_form4_all(xml):
                t["url"] = folder + "/"
                out.append(t)
        except ET.ParseError:
            pass
    out.sort(key=lambda t: t["date"], reverse=True)
    return out


# ---------- putting it together ----------

def ticker_map(ua):
    data = get_json("https://www.sec.gov/files/company_tickers.json", ua) or {}
    return {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in data.values()}


def build(ticker, cik, ua, now):
    subs = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", ua)
    if not subs:
        raise RuntimeError("no submissions data")
    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", ua)
    name = subs.get("name", ticker)
    tenk = latest_10k(subs)
    rows, folder = filing_concentration(cik, tenk["accession"], ua) if tenk else ([], "")
    fin = financials(facts)
    rev_latest = fin["revenue"][-1][1] if fin["revenue"] else None
    for r in rows:   # fill in a dollar estimate when only a percent of revenue is reported
        r["estimated"] = False
        if r["amount"] is None and r["pct"] is not None and r["benchmark"] == "revenue" \
                and r["kind"] == "customer" and rev_latest:
            r["amount"], r["estimated"] = round(rev_latest * r["pct"] / 100), True
        r["named"] = is_named(r["name"])
    suppliers, search_name = find_suppliers(name, cik, ua, now)
    trades = insider_activity(cik, subs, ua)
    year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    recent = [t for t in trades if t["date"] >= year_ago]
    addr = subs.get("addresses", {}).get("business", {})
    return {
        "ticker": ticker, "cik": cik, "name": name, "searchName": search_name,
        "updated": now.isoformat(timespec="seconds"),
        "profile": {
            "industry": subs.get("sicDescription", ""), "sic": subs.get("sic", ""),
            "exchanges": subs.get("exchanges", []), "tickers": subs.get("tickers", []),
            "state": subs.get("stateOfIncorporation", ""),
            "city": ", ".join(x for x in (addr.get("city"), addr.get("stateOrCountry")) if x),
            "fiscalYearEnd": subs.get("fiscalYearEnd", ""), "website": subs.get("website", ""),
            "formerNames": [f.get("name") for f in subs.get("formerNames", [])][:3],
        },
        "financials": fin,
        "tenK": dict(tenk, url=folder + "/") if tenk else None,
        "customers": [r for r in rows if r["kind"] == "customer"],
        "supplierConcentration": [r for r in rows if r["kind"] == "supplier"],
        "suppliers": suppliers,
        "insiders": {
            "trades": trades,
            "bought12m": sum(t["value"] for t in recent if t["type"] == "buy"),
            "sold12m": sum(t["value"] for t in recent if t["type"] == "sell"),
            "buyers12m": len({t["owner"] for t in recent if t["type"] == "buy"}),
            "sellers12m": len({t["owner"] for t in recent if t["type"] == "sell"}),
        },
    }


def auto_tickers(index, now):
    db = load_json("data/filings.json", {"rows": []})
    cutoff = (now - timedelta(days=AUTO_DAYS)).isoformat()
    fresh = (now - timedelta(days=REFRESH_DAYS)).isoformat()
    totals = {}
    for r in db.get("rows", []):
        if r.get("filed", "") >= cutoff and r.get("ticker") not in ("", "NONE", "N/A"):
            totals[r["ticker"]] = totals.get(r["ticker"], 0) + r.get("value", 0)
    picks = [t for t, v in sorted(totals.items(), key=lambda kv: -kv[1])
             if v >= AUTO_MIN_VALUE and index.get(t, {}).get("updated", "") < fresh]
    return picks[:AUTO_MAX]


def run(ua, tickers, now=None):
    now = now or datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)
    index = load_json(INDEX_FILE, {})
    tickers = [t.strip().upper() for t in tickers if t.strip()] or auto_tickers(index, now)
    if not tickers:
        print("nothing to do")
        return
    tmap = ticker_map(ua)
    done = 0
    for t in tickers:
        if t not in tmap:
            print(f"{t}: not found in the SEC ticker list")
            continue
        cik, _ = tmap[t]
        try:
            d = build(t, cik, ua, now)
        except Exception as e:   # one bad company should not stop the rest
            print(f"{t}: failed ({e})")
            continue
        with open(f"{OUT_DIR}/{t}.json", "w") as f:
            json.dump(d, f, separators=(",", ":"))
        index[t] = {"name": d["name"], "updated": d["updated"], "customers": len(d["customers"]),
                    "suppliers": sum(1 for s in d["suppliers"] if s["verified"])}
        done += 1
        print(f"{t}: {len(d['customers'])} customer rows, {index[t]['suppliers']} verified suppliers "
              f"of {len(d['suppliers'])} checked, {len(d['insiders']['trades'])} insider trades")
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, separators=(",", ":"), sort_keys=True)
    print(f"built {done} of {len(tickers)} deep dives")


if __name__ == "__main__":
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        sys.exit("Set SEC_USER_AGENT to a name and contact email, e.g. 'My Insider Tracker you@example.com'.")
    args = sys.argv[1:] or re.split(r"[\s,]+", os.environ.get("TICKERS", ""))
    run(ua, args)
