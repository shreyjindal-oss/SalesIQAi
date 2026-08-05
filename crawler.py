"""
Daily crawl pipeline — all data sources, ported 1:1 from the reference
implementation. Fetches official/open feeds, applies verbatim keyword/CPV
filters (no inference), and writes one JSON document per board to Datastore.

Sources (all free, no key except optional GNews for the news-watch):
  - GOV.UK Residential Property Tribunal decisions (Building Safety Act) + content API
  - Upper Tribunal (Lands Chamber) via National Archives Find Case Law atom feed
  - Environment Agency flood-monitoring API
  - Find a Tender Service OCDS API (housing tenders, infra wins, London/UK projects,
    prospect matches against the account roster)
  - Google Sheet CSV (account roster)
  - GNews.io search (HQ/office-move news-watch)
"""
import csv
import io
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

import store
from analyzer import (
    clean, extract_evidence, status_from_evidence, extract_deadlines,
    match_priority, pretty_sub, STATUS,
)
from config import CONFIG

UA = {"User-Agent": "SalesIntelligenceIQ/1.0 (thesqua.re accommodation-demand monitoring)"}
SOURCE_FTT = "First-tier Tribunal (GOV.UK)"
SOURCE_UT = "Upper Tribunal (Lands Chamber)"

SEARCH_URL = ("https://www.gov.uk/api/search.json"
              "?filter_format=residential_property_tribunal_decision"
              "&filter_tribunal_decision_category=building-safety-act"
              "&count=200&fields=title,link,public_timestamp")
LISTING_URL = ("https://www.gov.uk/residential-property-tribunal-decisions"
               "?tribunal_decision_category%5B%5D=building-safety-act")


def _now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url, headers=None, timeout=30):
    return requests.get(url, headers=headers or UA, timeout=timeout, allow_redirects=True)


# ============================================================================
# TRIBUNAL — First-tier (GOV.UK) + Upper Tribunal (National Archives)
# ============================================================================
CASE_REF_RE = re.compile(r"\b(?:LON|MAN|CAM|BIR|HAV|CHI|MID|NOR)\/\S+?\/\d{4}\/\d{4}\b")
_TAG_RE = re.compile(r"<[^>]+>")


def build_case(item, content, prev_case, run_ts):
    slug = item["link"].rsplit("/", 1)[-1]
    details = (content or {}).get("details") or {}
    meta = details.get("metadata") or {}
    attachments = [a.get("url") for a in (details.get("attachments") or []) if a.get("url")]
    hidden = meta.get("hidden_indexable_content") or ""
    body = _TAG_RE.sub(" ", str(details.get("body") or ""))
    title = clean((content or {}).get("title") or item.get("title") or "")
    sub_category = pretty_sub(meta.get("tribunal_decision_sub_category"))
    evidence = extract_evidence(" ".join([title, body, hidden]))
    status = status_from_evidence(evidence, True)
    c = {
        "slug": slug,
        "url": "https://www.gov.uk" + item["link"],
        "title": title,
        "case_refs": CASE_REF_RE.findall(title),
        "sub_category": sub_category,
        "decision_date": meta.get("tribunal_decision_decision_date") or "",
        "public_timestamp": item.get("public_timestamp") or "",
        "attachments": attachments,
        "source": SOURCE_FTT,
        "priority": match_priority(title, sub_category),
        "decant": {"status": status, "evidence": evidence,
                   "dates_in_evidence": extract_deadlines(evidence)},
        "first_seen": (prev_case or {}).get("first_seen") or run_ts,
        "last_changed": run_ts,
        "is_new": not prev_case,
        "changed_fields": [],
    }
    if prev_case:
        for f in ("decision_date", "public_timestamp", "sub_category"):
            if (prev_case.get(f) or "") != c[f]:
                c["changed_fields"].append(f)
        if (prev_case.get("decant") or {}).get("status") != status:
            c["changed_fields"].append("decant_status")
        pa, na = set(prev_case.get("attachments") or []), set(attachments)
        if na and pa and (len(na) != len(pa) or any(u not in pa for u in na)):
            c["changed_fields"].append("new_document")
        if not c["changed_fields"]:
            c["last_changed"] = prev_case.get("last_changed") or run_ts
    return c


UT_ATOM = ("https://caselaw.nationalarchives.gov.uk/atom.xml"
           "?court=ukut/lc&query=building+safety&order=-date&per_page=50")
UT_MAX_DETAIL = 12
POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
COURT_POSTCODES = {"EC4A1NL", "WC2A2LL", "EC4A1DZ", "M602LP", "M13FY"}


def _unxml(s):
    return (str(s).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))


def _building_postcode(text):
    seen = set()
    for m in POSTCODE_RE.finditer(text):
        pc = re.sub(r"\s+", "", m.group(0)).upper()
        if pc in COURT_POSTCODES or pc in seen:
            continue
        seen.add(pc)
        return m.group(0).strip()
    return ""


def _citation_from_url(url):
    m = re.search(r"ukut/lc/(\d{4})/(\d+)", url)
    return "[%s] UKUT %s (LC)" % (m.group(1), m.group(2)) if m else ""


def _pc_key(pc):
    return re.sub(r"\s+", "", pc).upper()


def parse_ut_feed(xml):
    entries = []
    for block in xml.split("<entry>")[1:]:
        title = (re.search(r"<title>([\s\S]*?)</title>", block) or [None, ""])
        title = title.group(1) if hasattr(title, "group") else ""
        href_m = re.search(r'<link[^>]*href="([^"]+)"', block)
        href = href_m.group(1) if href_m else ""
        pub_m = re.search(r"<published>([^<]+)</published>", block)
        pub = pub_m.group(1) if pub_m else ""
        hash_m = re.search(r"<tna:contenthash>([^<]+)</tna:contenthash>", block)
        chash = hash_m.group(1) if hash_m else ""
        if not href:
            continue
        m = re.search(r"ukut/lc/(\d{4})/(\d+)", href)
        slug = ("ukut-lc-%s-%s" % (m.group(1), m.group(2))) if m else "-".join(href.split("/")[-3:])
        entries.append({"title": _unxml(clean(title)), "url": href, "published": pub,
                        "contenthash": chash, "slug": slug})
    return entries


def build_ut_case(entry, judgment_text, prev_case, run_ts, pc_index):
    text = clean(_TAG_RE.sub(" ", str(judgment_text)))
    if not re.search(r"building\s+safety\s+act", text, re.I):
        return None
    evidence = extract_evidence(" ".join([entry["title"], text]))
    status = status_from_evidence(evidence, True)
    pc = _building_postcode(text)
    priority = match_priority(entry["title"], "")
    if priority["value"] == "Unrated" and pc and pc_index.get(_pc_key(pc)):
        priority = {"value": pc_index[_pc_key(pc)]["value"],
                    "source": "linked to First-tier Tribunal case by building postcode %s" % pc}
    c = {
        "slug": entry["slug"], "url": entry["url"], "title": entry["title"],
        "case_refs": [x for x in [_citation_from_url(entry["url"])] if x],
        "sub_category": "Upper Tribunal (Lands Chamber) appeal",
        "decision_date": (entry.get("published") or "")[:10],
        "public_timestamp": entry.get("published") or "",
        "attachments": [entry["url"]], "address_hint": pc, "source": SOURCE_UT,
        "contenthash": entry.get("contenthash"), "priority": priority,
        "decant": {"status": status, "evidence": evidence,
                   "dates_in_evidence": extract_deadlines(evidence)},
        "first_seen": (prev_case or {}).get("first_seen") or run_ts,
        "last_changed": run_ts, "is_new": not prev_case, "changed_fields": [],
    }
    if prev_case:
        if (prev_case.get("contenthash") or "") != c["contenthash"]:
            c["changed_fields"].append("judgment_updated")
        if (prev_case.get("decant") or {}).get("status") != status:
            c["changed_fields"].append("decant_status")
        if not c["changed_fields"]:
            c["last_changed"] = prev_case.get("last_changed") or run_ts
    return c


def crawl_upper_tribunal(prev_map, run_ts):
    out = {"cases": [], "skipped": 0, "fetched": 0}
    try:
        res = _get(UT_ATOM)
        if not res.ok:
            raise RuntimeError("UT feed HTTP %s" % res.status_code)
        xml = res.text
    except Exception as e:
        for p in prev_map.values():
            if p.get("source") == SOURCE_UT:
                out["cases"].append({**p, "is_new": False, "changed_fields": []})
        out["error"] = str(e)
        return out

    seen = store.get_json("ut_seen", {}) or {}
    pc_index = {}
    for p in prev_map.values():
        if (p.get("source") or SOURCE_FTT) != SOURCE_FTT:
            continue
        m = re.search(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", p.get("title") or "")
        if m:
            pc_index[_pc_key(m.group(0))] = p.get("priority")

    entries = parse_ut_feed(xml)
    todo = []
    for e in entries:
        prev = prev_map.get(e["slug"])
        if prev and (prev.get("contenthash") or "") == e["contenthash"]:
            out["cases"].append({**prev, "is_new": False, "changed_fields": []})
        elif seen.get(e["slug"]) == e["contenthash"]:
            continue
        else:
            todo.append(e)

    for e in todo[:UT_MAX_DETAIL]:
        try:
            res = _get(e["url"])
            if not res.ok:
                raise RuntimeError("HTTP %s" % res.status_code)
            out["fetched"] += 1
            c = build_ut_case(e, res.text, prev_map.get(e["slug"]), run_ts, pc_index)
            seen[e["slug"]] = e["contenthash"]
            if c:
                out["cases"].append(c)
        except Exception:
            prev = prev_map.get(e["slug"])
            if prev:
                out["cases"].append({**prev, "is_new": False, "changed_fields": []})
            out["skipped"] += 1
    out["skipped"] += max(0, len(todo) - UT_MAX_DETAIL)
    store.put_json("ut_seen", seen)
    return out


def compute_alerts(cases, prev_map):
    new_cases = [c for c in cases if c.get("is_new")]
    new_signals = []
    for c in cases:
        p = prev_map.get(c["slug"])
        st = c["decant"]["status"]
        if p and (p.get("decant") or {}).get("status") != st and st in (STATUS["DECANT"], STATUS["ACCOM"]):
            new_signals.append(c)
    updated = [c for c in cases if not c.get("is_new") and c.get("changed_fields")]
    return {"newCases": new_cases, "newSignals": new_signals, "updated": updated}


# ============================================================================
# FLOODS — Environment Agency flood-monitoring API
# ============================================================================
FLOOD_API = "https://environment.data.gov.uk/flood-monitoring/id/floods"
FLOOD_SEV = {1: "Severe Flood Warning", 2: "Flood Warning", 3: "Flood Alert", 4: "No longer in force"}
FLOOD_CONTACTS = {
    "Home & contents insurers (fund alternative accommodation)":
        ["Aviva", "Direct Line Group", "Admiral", "LV=", "Ageas", "AXA UK", "Zurich UK",
         "Allianz UK", "RSA", "NFU Mutual", "Saga"],
    "Loss adjusters / claims managers (place displaced policyholders)":
        ["Sedgwick", "Crawford & Company", "Davies Group", "McLarens", "QuestGates",
         "Woodgate & Clark", "Claims Consortium Group"],
    "Flood restoration / disaster recovery (first on site)":
        ["Rainbow Restoration", "ServiceMaster Restore", "Polygon UK", "ISS Damage Control"],
    "Public sector & market bodies":
        ["Local Resilience Forum (per region)", "Council emergency planning / housing team",
         "Flood Re (reinsurer)", "ABI (market data)"],
}


def build_flood_lead(item, prev, run_ts):
    fa = item.get("floodArea") or {}
    try:
        sev = int(item.get("severityLevel") or 0)
    except (TypeError, ValueError):
        sev = 0
    fid = item.get("floodAreaID") or fa.get("notation") or str(item.get("@id") or "").rsplit("/", 1)[-1]
    c = {
        "id": fid,
        "area": clean(item.get("description") or fa.get("riverOrSea") or fid),
        "county": fa.get("county") or "", "river": fa.get("riverOrSea") or "",
        "region": item.get("eaAreaName") or "", "tidal": bool(item.get("isTidal")),
        "severity": item.get("severity") or FLOOD_SEV.get(sev, ""), "severity_level": sev,
        "message": clean(item.get("message") or ""),
        "raised": item.get("timeRaised") or "",
        "updated": item.get("timeMessageChanged") or item.get("timeSeverityChanged") or item.get("timeRaised") or "",
        "source": "Environment Agency flood-monitoring",
        "url": "https://check-for-flooding.service.gov.uk/",
        "first_seen": (prev or {}).get("first_seen") or run_ts,
        "last_changed": run_ts, "is_new": not prev, "changed_fields": [],
    }
    if prev:
        if (prev.get("severity_level") or 0) != sev:
            c["changed_fields"].append("severity")
        if (prev.get("message") or "") != c["message"]:
            c["changed_fields"].append("message")
        if not c["changed_fields"]:
            c["last_changed"] = prev.get("last_changed") or run_ts
    return c


def crawl_floods(run_ts):
    prev_data = store.get_json("floods")
    prev_map = {l["id"]: l for l in (prev_data or {}).get("leads", [])}
    try:
        res = _get(FLOOD_API + "?min-severity=3")
        if not res.ok:
            raise RuntimeError("EA flood API HTTP %s" % res.status_code)
        data = res.json()
    except Exception as e:
        if prev_data:
            prev_data["error"] = str(e)
            return prev_data
        return {"generated_at": run_ts, "leads": [], "new": [], "escalated": [], "count": 0, "error": str(e)}

    leads, new_ids, escalated = [], [], []
    for it in data.get("items", []):
        prev = prev_map.get(it.get("floodAreaID") or (it.get("floodArea") or {}).get("notation"))
        lead = build_flood_lead(it, prev, run_ts)
        leads.append(lead)
        if lead["is_new"]:
            new_ids.append(lead["id"])
        elif prev and lead["severity_level"] < prev["severity_level"]:
            escalated.append(lead["id"])
    leads.sort(key=lambda l: (l["severity_level"], _neg_str(l.get("updated") or "")))
    out = {
        "generated_at": run_ts, "source": "https://check-for-flooding.service.gov.uk/",
        "api": FLOOD_API, "count": len(leads),
        "severe": sum(1 for l in leads if l["severity_level"] == 1),
        "warnings": sum(1 for l in leads if l["severity_level"] == 2),
        "alerts": sum(1 for l in leads if l["severity_level"] == 3),
        "new": new_ids, "escalated": escalated, "contacts": FLOOD_CONTACTS, "leads": leads,
    }
    store.put_json("floods", out)
    return out


class _neg_str:
    """Sort helper: descending string order within an ascending sort key."""
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s

    def __lt__(self, other):
        return self.s > other.s

    def __eq__(self, other):
        return self.s == other.s


# ============================================================================
# FIND A TENDER — housing tenders, infra wins, London/UK projects, prospects
# ============================================================================
FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?limit=100"
TENDER_MAX_PAGES = 6
TENDER_TERMS = [
    "temporary accommodation", "emergency accommodation", "bridging accommodation",
    "interim accommodation", "alternative accommodation", "short-term accommodation",
    "supported accommodation", "move-on accommodation", "serviced apartment",
    "serviced accommodation", "service family accommodation", "service families accommodation",
    "dispersal accommodation", "asylum accommodation", "asylum seeker accommodation",
    "homelessness", "homeless households", "decant", "nightly paid", "bed and breakfast",
]
TENDER_RE = re.compile("(" + "|".join(r"\s+".join(re.escape(w) for w in t.split()) for t in TENDER_TERMS) + ")", re.I)
TENDER_CPV = ["55000000", "55100000", "55110000", "55250000", "55270000", "70000000",
              "70200000", "70300000", "70310000", "98000000", "98340000", "45211000", "45211100"]
INFRA_MIN_VALUE = 10_000_000
INFRA_CPV = ["45", "71", "342", "345", "44", "76"]
LONDON_MIN_VALUE = 1_000_000
LONDON_RE = re.compile(r"\blondon\b", re.I)


def _parties(rel, role):
    return [p for p in (rel.get("parties") or []) if role in (p.get("roles") or [])]


def _buyer(rel):
    b = _parties(rel, "buyer")
    return b[0] if b else {}


def _notice_url(rel):
    docs = []
    for c in (rel.get("contracts") or []):
        docs += (c.get("documents") or [])
    for d in docs:
        if "/Notice/" in (d.get("url") or ""):
            return d["url"]
    return "https://www.find-tender.service.gov.uk/Notice/" + (rel.get("id") or "")


def build_tender_lead(rel, prev, run_ts):
    t = rel.get("tender") or {}
    buyer = _buyer(rel)
    sup = _parties(rel, "supplier")
    cpv = []

    def collect(items):
        for it in (items or []):
            classes = (it.get("additionalClassifications") or [])
            if it.get("classification"):
                classes = classes + [it["classification"]]
            for c in classes:
                if c and c.get("scheme") == "CPV" and not any(x["id"] == c.get("id") for x in cpv):
                    cpv.append({"id": c.get("id"), "description": c.get("description")})
    collect(t.get("items"))
    for a in (rel.get("awards") or []):
        collect(a.get("items"))

    text = " ".join([x for x in [t.get("title"), t.get("description")] + [c.get("description") for c in cpv] if x])
    kw = sorted({re.sub(r"\s+", " ", m).lower() for m in TENDER_RE.findall(text)})
    cpv_hit = [c for c in cpv if any((c.get("id") or "").startswith(code[:6]) for code in TENDER_CPV)]
    if not kw and not cpv_hit:
        return None

    tag = rel.get("tag") or []
    if "award" in tag or "contract" in tag:
        stage = "Awarded"
    elif "tender" in tag:
        stage = "Open tender"
    elif "planning" in tag:
        stage = "Preliminary / market engagement"
    else:
        stage = {"planning": "Preliminary / market engagement", "tender": "Open tender",
                 "award": "Awarded", "contract": "Awarded"}.get(t.get("status"), t.get("status") or "")
    val = t.get("value") or ((rel.get("contracts") or [{}])[0].get("value")) or {}
    amount = val.get("amount") if val.get("amount") is not None else val.get("amountGross")
    items0 = (t.get("items") or [{}])[0]
    da = (items0.get("deliveryAddresses") or [{}])[0] if items0 else {}
    c = {
        "id": rel.get("id") or rel.get("ocid"), "ocid": rel.get("ocid") or "",
        "title": clean(t.get("title") or "(untitled notice)"),
        "description": clean(t.get("description") or "")[:500],
        "buyer": buyer.get("name") or (rel.get("buyer") or {}).get("name") or "",
        "buyer_email": (buyer.get("contactPoint") or {}).get("email") or "",
        "locality": (buyer.get("address") or {}).get("locality") or "",
        "region": da.get("region") or (buyer.get("address") or {}).get("region") or "",
        "stage": stage, "status": t.get("status") or "",
        "supplier": sup[0].get("name") if sup else "",
        "value_amount": amount, "value_currency": val.get("currency") or "GBP",
        "deadline": ((t.get("tenderPeriod") or {}).get("endDate") or ""),
        "published": rel.get("date") or "", "cpv": cpv,
        "matched": {"keywords": kw, "cpv": [(c.get("id") or "") + " " + (c.get("description") or "") for c in cpv_hit]},
        "url": _notice_url(rel), "source": "Find a Tender (FTS)",
        "first_seen": (prev or {}).get("first_seen") or run_ts,
        "last_changed": run_ts, "is_new": not prev, "changed_fields": [],
    }
    if prev:
        for f in ("stage", "status", "value_amount", "deadline", "supplier"):
            if (prev.get(f) if prev.get(f) is not None else "") != (c[f] if c[f] is not None else ""):
                c["changed_fields"].append(f)
        if not c["changed_fields"]:
            c["last_changed"] = prev.get("last_changed") or run_ts
    return c


def build_infra_win(rel, prev, run_ts):
    tag = rel.get("tag") or []
    if "award" not in tag and "contract" not in tag:
        return None
    t = rel.get("tender") or {}
    buyer = _buyer(rel)
    suppliers = list(dict.fromkeys(p.get("name") for p in _parties(rel, "supplier") if p.get("name")))
    if not suppliers:
        return None
    cpv = []

    def collect(items):
        for it in (items or []):
            for c in (it.get("additionalClassifications") or []):
                if c and c.get("scheme") == "CPV" and not any(x["id"] == c.get("id") for x in cpv):
                    cpv.append({"id": c.get("id"), "description": c.get("description")})
    collect(t.get("items"))
    for a in (rel.get("awards") or []):
        collect(a.get("items"))
    cat = t.get("mainProcurementCategory") or ((rel.get("awards") or [{}])[0].get("mainProcurementCategory")) or ""
    is_infra = cat == "works" or any(any((c.get("id") or "").startswith(p) for p in INFRA_CPV) for c in cpv)
    if not is_infra:
        return None
    val = ((rel.get("contracts") or [{}])[0].get("value")) or t.get("value") or {}
    amount = val.get("amount") if val.get("amount") is not None else val.get("amountGross")
    if amount is None or amount < INFRA_MIN_VALUE:
        return None
    return {
        "id": rel.get("id") or rel.get("ocid"), "title": clean(t.get("title") or "(untitled award)"),
        "buyer": buyer.get("name") or "", "suppliers": suppliers,
        "region": (buyer.get("address") or {}).get("region") or "",
        "locality": (buyer.get("address") or {}).get("locality") or "",
        "value_amount": amount, "value_currency": val.get("currency") or "GBP", "category": cat,
        "cpv": [(c.get("id") or "") + " " + (c.get("description") or "") for c in cpv][:4],
        "date": rel.get("date") or "", "url": _notice_url(rel),
        "source": "Find a Tender (FTS) — award",
        "first_seen": (prev or {}).get("first_seen") or run_ts, "is_new": not prev,
    }


def _is_london_award(rel):
    def hit(o):
        if not o:
            return False
        region = str(o.get("region") or "")
        return (region.upper().startswith("UKI") or LONDON_RE.search(region)
                or LONDON_RE.search(o.get("locality") or "") or LONDON_RE.search(o.get("postalCode") or ""))
    if hit((_buyer(rel).get("address") or {})):
        return True
    for aw in (rel.get("awards") or []):
        for it in (aw.get("items") or []):
            for d in (it.get("deliveryAddresses") or []):
                if hit(d):
                    return True
    return False


def _build_area_project(rel, prev, run_ts, want_london):
    tag = rel.get("tag") or []
    if "award" not in tag and "contract" not in tag:
        return None
    is_lon = _is_london_award(rel)
    if (not is_lon) if want_london else is_lon:
        return None
    t = rel.get("tender") or {}
    buyer = _buyer(rel)
    suppliers = list(dict.fromkeys(p.get("name") for p in _parties(rel, "supplier") if p.get("name")))
    if not suppliers:
        return None
    val = ((rel.get("contracts") or [{}])[0].get("value")) or t.get("value") or {}
    amount = val.get("amount") if val.get("amount") is not None else val.get("amountGross")
    if amount is None or amount < LONDON_MIN_VALUE:
        return None
    return {
        "id": rel.get("id") or rel.get("ocid"), "title": clean(t.get("title") or "(untitled award)"),
        "buyer": buyer.get("name") or "", "suppliers": suppliers,
        "locality": (buyer.get("address") or {}).get("locality") or "",
        "region": (buyer.get("address") or {}).get("region") or "",
        "value_amount": amount, "value_currency": val.get("currency") or "GBP",
        "category": t.get("mainProcurementCategory") or ((rel.get("awards") or [{}])[0].get("mainProcurementCategory")) or "",
        "date": rel.get("date") or "", "url": _notice_url(rel),
        "source": "Find a Tender (FTS) — " + ("London award" if want_london else "UK (ex-London) award"),
        "first_seen": (prev or {}).get("first_seen") or run_ts, "is_new": not prev,
    }


# ---- prospect matching (roster × award winners) ----
_LEGAL_RE = re.compile(
    r"\b(ltd|limited|plc|llp|lp|inc|incorporated|corp|corporation|co|company|group|holdings|"
    r"holding|international|intl|global|uk|the|and|services|service|solutions|consulting|"
    r"consultants|partnership|partners)\b", re.I)


def normalize_company(name):
    s = unicodedata.normalize("NFKD", str(name or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _LEGAL_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_roster_index(roster):
    exact, by_token = {}, []
    for r in roster or []:
        norm = normalize_company(r.get("account"))
        if not norm:
            continue
        if norm not in exact:
            exact[norm] = r
        tokens = [t for t in norm.split(" ") if len(t) >= 4]
        if len(tokens) >= 2:
            by_token.append({"norm": norm, "tokens": set(tokens), "rec": r})
    return {"exact": exact, "by_token": by_token}


def match_roster(idx, supplier_name):
    norm = normalize_company(supplier_name)
    if not norm:
        return None
    if norm in idx["exact"]:
        return {"rec": idx["exact"][norm], "match_type": "exact"}
    sup_tokens = {t for t in norm.split(" ") if len(t) >= 4}
    if len(sup_tokens) < 2:
        return None
    for cand in idx["by_token"]:
        if cand["tokens"].issubset(sup_tokens):
            return {"rec": cand["rec"], "match_type": "partial"}
    return None


def build_prospect_lead(rel, supplier_name, match, prev, run_ts):
    t = rel.get("tender") or {}
    buyer = _buyer(rel)
    val = ((rel.get("contracts") or [{}])[0].get("value")) or t.get("value") or {}
    amount = val.get("amount") if val.get("amount") is not None else val.get("amountGross")
    cat = t.get("mainProcurementCategory") or ((rel.get("awards") or [{}])[0].get("mainProcurementCategory")) or ""
    return {
        "id": (rel.get("id") or rel.get("ocid") or "") + "::" + normalize_company(supplier_name),
        "account": match["rec"].get("account"), "owner": match["rec"].get("owner"),
        "domain": match["rec"].get("domain") or "",
        "matched_supplier": supplier_name, "match_type": match["match_type"],
        "contract_title": clean(t.get("title") or "(untitled award)"),
        "buyer": buyer.get("name") or (rel.get("buyer") or {}).get("name") or "",
        "locality": (buyer.get("address") or {}).get("locality") or "",
        "value_amount": amount, "value_currency": val.get("currency") or "GBP",
        "category": cat, "date": rel.get("date") or "", "url": _notice_url(rel),
        "source": "Find a Tender (FTS) — award × account roster",
        "first_seen": (prev or {}).get("first_seen") or run_ts, "is_new": not prev,
    }


def crawl_tenders(run_ts):
    prev_data = store.get_json("tenders")
    prev_map = {l["id"]: l for l in (prev_data or {}).get("leads", [])}
    prev_infra = store.get_json("corp_infra")
    prev_infra_map = {l["id"]: l for l in (prev_infra or {}).get("leads", [])}
    roster_doc = store.get_json("roster")
    roster_idx = build_roster_index((roster_doc or {}).get("accounts") or []) if roster_doc else None
    prev_prosp = store.get_json("prospects")
    prev_prosp_map = {l["id"]: l for l in (prev_prosp or {}).get("leads", [])}

    found, infra_found, london_found, uk_found, prosp_found = {}, {}, {}, {}, {}
    url, pages, error = FTS_API, 0, None
    try:
        while url and pages < TENDER_MAX_PAGES:
            res = _get(url)
            if not res.ok:
                raise RuntimeError("FTS API HTTP %s" % res.status_code)
            pkg = res.json()
            for rel in pkg.get("releases") or []:
                lead = build_tender_lead(rel, prev_map.get(rel.get("id") or rel.get("ocid")), run_ts)
                if lead:
                    found[lead["id"]] = lead
                win = build_infra_win(rel, prev_infra_map.get(rel.get("id") or rel.get("ocid")), run_ts)
                if win:
                    infra_found[win["id"]] = win
                lp = _build_area_project(rel, None, run_ts, True)
                if lp:
                    london_found[lp["id"]] = lp
                up = _build_area_project(rel, None, run_ts, False)
                if up:
                    uk_found[up["id"]] = up
                if roster_idx:
                    tag = rel.get("tag") or []
                    if "award" in tag or "contract" in tag:
                        suppliers = list(dict.fromkeys(p.get("name") for p in _parties(rel, "supplier") if p.get("name")))
                        for s in suppliers:
                            m = match_roster(roster_idx, s)
                            if not m:
                                continue
                            key = (rel.get("id") or rel.get("ocid") or "") + "::" + normalize_company(s)
                            lead = build_prospect_lead(rel, s, m, prev_prosp_map.get(key), run_ts)
                            prosp_found[lead["id"]] = lead
            url = (pkg.get("links") or {}).get("next")
            pages += 1
    except Exception as e:
        error = str(e)

    for pid, prev in prev_map.items():
        found.setdefault(pid, {**prev, "is_new": False, "changed_fields": []})
    for pid, prev in prev_infra_map.items():
        infra_found.setdefault(pid, {**prev, "is_new": False})
    for pid, prev in prev_prosp_map.items():
        prosp_found.setdefault(pid, {**prev, "is_new": False})

    leads = sorted(found.values(), key=lambda l: _neg_str(l.get("published") or ""))
    out = {
        "generated_at": run_ts, "source": "https://www.find-tender.service.gov.uk/", "api": FTS_API,
        "count": len(leads), "pages_scanned": pages,
        "open": sum(1 for l in leads if l.get("stage") == "Open tender"),
        "awarded": sum(1 for l in leads if l.get("stage") == "Awarded"),
        "new": [l["id"] for l in leads if l.get("is_new")] if prev_data else [],
        "error": error, "leads": leads,
    }
    store.put_json("tenders", out)

    infra_leads = sorted(infra_found.values(), key=lambda l: _neg_str(l.get("date") or ""))
    infra_out = {"generated_at": run_ts, "source": "https://www.find-tender.service.gov.uk/",
                 "count": len(infra_leads),
                 "new": [l["id"] for l in infra_leads if l.get("is_new")] if prev_infra else [],
                 "leads": infra_leads}
    store.put_json("corp_infra", infra_out)

    prosp_leads = sorted(prosp_found.values(), key=lambda l: _neg_str(l.get("date") or ""))
    by_owner = {}
    for l in prosp_leads:
        by_owner[l.get("owner")] = by_owner.get(l.get("owner"), 0) + 1
    prosp_out = {"generated_at": run_ts, "source": "https://www.find-tender.service.gov.uk/",
                 "roster_size": len(roster_idx["exact"]) if roster_idx else 0, "count": len(prosp_leads),
                 "new": [l["id"] for l in prosp_leads if l.get("is_new")] if prev_prosp else [],
                 "by_owner": by_owner, "leads": prosp_leads}
    store.put_json("prospects", prosp_out)

    london = sorted(london_found.values(), key=lambda l: _neg_str(l.get("date") or ""))
    uk = sorted(uk_found.values(), key=lambda l: _neg_str(l.get("date") or ""))[:100]
    return {"tenders": out, "infra": infra_out, "prospects": prosp_out, "london": london, "uk": uk}


# ============================================================================
# ROSTER — Google Sheet (CSV export)
# ============================================================================
ROSTER_SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
                    "1Huefbi603aG5lNfJjQHiIcBXCEnEXPpotmElNfcUh9A/export?format=csv&gid=0")
ROSTER_LABELS = {"account": "account name", "owner": "account owner", "website": "website"}


def roster_domain(v):
    if not v:
        return ""
    s = str(v)
    m = re.search(r"https?://([^/\"'?\s]+)", s, re.I)
    host = (m.group(1) if m else s).lower()
    host = re.sub(r"^www\.", "", host)
    host = re.sub(r"[^a-z0-9.\-].*$", "", host)
    return host if "." in host else ""


def parse_roster_csv(text):
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c != "" for c in r)]
    if not rows:
        raise RuntimeError("empty CSV")
    hdr = [h.strip().lower() for h in rows[0]]
    try:
        i_acc = hdr.index(ROSTER_LABELS["account"])
    except ValueError:
        raise RuntimeError("CSV missing an 'Account Name' column")
    i_own = hdr.index(ROSTER_LABELS["owner"]) if ROSTER_LABELS["owner"] in hdr else -1
    i_web = hdr.index(ROSTER_LABELS["website"]) if ROSTER_LABELS["website"] in hdr else -1
    out, seen = [], set()
    for cells in rows[1:]:
        account = (cells[i_acc] if i_acc < len(cells) else "").strip()
        if not account:
            continue
        owner = (cells[i_own].strip() if 0 <= i_own < len(cells) else "")
        domain = roster_domain(cells[i_web]) if 0 <= i_web < len(cells) else ""
        key = account.lower() + "|" + owner.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"account": account, "owner": owner, "domain": domain})
    return out


def crawl_roster(run_ts):
    url = CONFIG["ROSTER_SHEET_URL"] or ROSTER_SHEET_URL
    try:
        res = _get(url, headers={"Accept": "text/csv,*/*"})
        if not res.ok:
            raise RuntimeError("sheet CSV HTTP %s" % res.status_code)
        text = res.text
        if re.search(r"<html", text[:200], re.I):
            raise RuntimeError("sheet not public (got HTML, not CSV) — set link sharing to 'anyone with the link can view'")
        accounts = parse_roster_csv(text)
        if not accounts:
            raise RuntimeError("sheet returned 0 accounts")
        store.put_json("roster", {"accounts": accounts, "uploaded_at": run_ts,
                                  "source": "Google Sheet (CSV export)", "sheet_url": url})
        return {"count": len(accounts)}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# NEWS — GNews.io search (HQ / office moves)
# ============================================================================
GNEWS_HQ_QUERY = 'London ("new office" OR "office opening" OR headquarters OR relocation OR "opens office" OR expansion)'
GNEWS_UK_QUERY = ('(UK OR Britain OR Manchester OR Birmingham OR Leeds OR Glasgow OR Bristol) '
                  '("new office" OR "office opening" OR headquarters OR relocation OR "opens office" OR expansion)')


def _gnews_url(q, key):
    to = (datetime.now(timezone.utc) - timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ")
    frm = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("https://gnews.io/api/v4/search?q=" + requests.utils.quote(q) +
            "&lang=en&country=gb&max=10&sortby=publishedAt&from=" + frm + "&to=" + to +
            "&apikey=" + requests.utils.quote(key))


def parse_gnews(payload, exclude_london):
    if isinstance(payload, str):
        try:
            import json as _json
            j = _json.loads(payload)
        except Exception:
            return []
    else:
        j = payload
    arts = (j or {}).get("articles") or []
    out, seen = [], set()
    for a in arts:
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        if exclude_london and LONDON_RE.search(title):
            continue
        key = re.sub(r"\s+", " ", title.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        src = a.get("source") or {}
        out.append({"title": title, "publisher": src.get("name") or "",
                    "publisher_url": src.get("url") or "", "link": a.get("url") or "",
                    "pubDate": str(a.get("publishedAt") or "")[:10], "query": ""})
    return out[:40]


def _fetch_news(query, exclude_london, key):
    if not key:
        return []
    try:
        res = _get(_gnews_url(query, key), headers={"Accept": "application/json"})
        if not res.ok:
            return []
        return parse_gnews(res.text, exclude_london)
    except Exception:
        return []


def _crawl_moves(dataset, run_ts, query, exclude_london, projects):
    prev = store.get_json(dataset)

    def keyf(s):
        return re.sub(r"\s+", " ", str(s).lower()).strip()
    prev_news = {keyf(n["title"]) for n in (prev or {}).get("news", [])}
    prev_proj = {p["id"] for p in (prev or {}).get("projects", [])}
    news = _fetch_news(query, exclude_london, CONFIG["GNEWS_API_KEY"])
    news = [{**n, "is_new": keyf(n["title"]) not in prev_news} for n in news]
    projects = [{**p, "is_new": p["id"] not in prev_proj} for p in (projects or [])]
    out = {
        "generated_at": run_ts, "news_count": len(news), "projects_count": len(projects),
        "news_new": sum(1 for n in news if n["is_new"]) if prev else 0,
        "projects_new": sum(1 for p in projects if p["is_new"]) if prev else 0,
        "news": news, "projects": projects,
    }
    store.put_json(dataset, out)
    return out


# ============================================================================
# ORCHESTRATOR
# ============================================================================
def crawl(full=False):
    run_ts = _now_ts()
    res = _get(SEARCH_URL)
    if not res.ok:
        raise RuntimeError("GOV.UK search API HTTP %s" % res.status_code)
    listing = res.json()

    prev_data = store.get_json("cases")
    prev_map = {c["slug"]: c for c in (prev_data or {}).get("cases", [])}

    cases, todo = [], []
    for item in listing.get("results") or []:
        slug = item["link"].rsplit("/", 1)[-1]
        prev = prev_map.get(slug)
        if full or not prev or (prev.get("public_timestamp") or "") != (item.get("public_timestamp") or ""):
            todo.append(item)
        else:
            cases.append({**prev, "sub_category": pretty_sub(prev.get("sub_category")),
                          "source": prev.get("source") or SOURCE_FTT, "is_new": False, "changed_fields": []})

    cap = min(CONFIG["DETAIL_CAP"], 200)
    skipped = 0
    for item in todo[:cap]:
        slug = item["link"].rsplit("/", 1)[-1]
        try:
            r = _get("https://www.gov.uk/api/content" + item["link"])
            if not r.ok:
                raise RuntimeError("HTTP %s" % r.status_code)
            cases.append(build_case(item, r.json(), prev_map.get(slug), run_ts))
        except Exception:
            prev = prev_map.get(slug)
            if prev:
                cases.append({**prev, "source": prev.get("source") or SOURCE_FTT, "is_new": False, "changed_fields": []})
            skipped += 1
    skipped += max(0, len(todo) - cap)

    ut = crawl_upper_tribunal(prev_map, run_ts)
    cases += ut["cases"]
    skipped += ut["skipped"]

    cases.sort(key=lambda c: _neg_str(c.get("public_timestamp") or ""))
    alerts = compute_alerts(cases, prev_map)
    is_baseline = not prev_data

    data = {
        "generated_at": run_ts, "source": LISTING_URL, "case_count": len(cases),
        "new_in_this_run": [] if is_baseline else [c["slug"] for c in alerts["newCases"]],
        "updated_in_this_run": [c["slug"] for c in alerts["updated"]],
        "cases": cases,
    }
    store.put_json("cases", data)

    log = store.get_json("changelog", []) or []
    log.append({"run": run_ts, "total": len(cases),
                "new_cases": (["baseline import of %d cases" % len(cases)] if is_baseline
                              else [c["slug"] for c in alerts["newCases"]]),
                "updated_cases": [c["slug"] for c in alerts["updated"]], "skipped": skipped})
    store.put_json("changelog", log[-90:])

    roster = crawl_roster(run_ts)
    floods = crawl_floods(run_ts)
    tenders = crawl_tenders(run_ts)
    hq = _crawl_moves("hq", run_ts, GNEWS_HQ_QUERY, False, tenders["london"])
    ukm = _crawl_moves("ukmoves", run_ts, GNEWS_UK_QUERY, True, tenders["uk"])

    email_result = "skipped"
    corp_alerts = (len(tenders["infra"].get("new", [])) + len(tenders["prospects"].get("new", []))
                   + hq["projects_new"] + hq["news_new"] + ukm["projects_new"] + ukm["news_new"])
    has_alerts = (len(alerts["newCases"]) or len(alerts["newSignals"]) or len(alerts["updated"])
                  or len(floods.get("new", [])) + len(floods.get("escalated", []))
                  or len(tenders["tenders"].get("new", [])) or corp_alerts)
    if CONFIG["SENDGRID_API_KEY"] and not is_baseline and (CONFIG["EMAIL_MODE"] == "always" or has_alerts):
        import emailer
        email_result = emailer.send_digest(data, alerts, floods, tenders,
                                            {"hq": hq, "uk": ukm})

    return {
        "run": run_ts, "total": len(cases), "ut_fetched": ut["fetched"],
        "ut_error": ut.get("error"),
        "floods_count": floods.get("count", 0), "floods_new": len(floods.get("new", [])),
        "floods_error": floods.get("error"),
        "tenders_count": tenders["tenders"].get("count", 0), "tenders_new": len(tenders["tenders"].get("new", [])),
        "tenders_error": tenders["tenders"].get("error"),
        "infra_count": tenders["infra"].get("count", 0),
        "prospects_count": tenders["prospects"].get("count", 0),
        "roster_count": roster.get("count"), "roster_error": roster.get("error"),
        "hq_news": hq["news_count"], "hq_projects": hq["projects_count"],
        "uk_news": ukm["news_count"], "uk_projects": ukm["projects_count"],
        "skipped": skipped, "new_cases": len(alerts["newCases"]),
        "new_signals": len(alerts["newSignals"]), "updated": len(alerts["updated"]),
        "email": email_result, "baseline": is_baseline,
    }
