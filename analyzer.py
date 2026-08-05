"""
Tribunal decant-signal analyzer.

STRICT NO-INFERENCE POLICY: every status derives ONLY from verbatim keyword
matches in the official GOV.UK decision text. Each match is stored as a quoted
snippet so a human can verify it. Nothing is guessed.
"""
import re

DECANT_TERMS = {"decant": r"decant\w*"}
ACCOMMODATION_TERMS = {
    "temporary accommodation": r"temporary\s+accommodation",
    "alternative accommodation": r"alternative\s+accommodation",
    "emergency accommodation": r"emergency\s+accommodation",
    "rehousing": r"re-?hous\w+",
    "evacuation": r"evacuat\w+",
}
OCCUPANCY_RISK_TERMS = {
    "prohibition notice": r"prohibition\s+notice",
    "vacate": r"vacat\w+",
    "waking watch": r"waking\s+watch",
    "hotel": r"hotels?\b",
    "uninhabitable": r"uninhabitable",
}
ALL_TERMS = {**DECANT_TERMS, **ACCOMMODATION_TERMS, **OCCUPANCY_RISK_TERMS}

STATUS = {
    "DECANT": "DECANT REFERENCED IN DECISION",
    "ACCOM": "ACCOMMODATION SIGNAL IN DECISION",
    "RISK": "OCCUPANCY-RISK SIGNAL",
    "NONE": "NO DECANT SIGNAL FOUND",
    "UNSCANNED": "DECISION TEXT NOT YET SCANNED",
}

_WINDOW = 170
_MAX_SNIPPETS = 6
_DATE_RE = re.compile(
    r"\b(?:by\s+)?(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+20\d{2})\b", re.I)


def clean(text):
    s = str(text or "")
    for a, b in (("\\r", " "), ("\\n", " "), ('\\"', '"'), ("\\t", " "),
                 ("\\/", "/"), ("\\u00a3", "£")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def extract_evidence(text):
    text = clean(text)
    out = {}
    for term, pattern in ALL_TERMS.items():
        snippets, seen = [], set()
        for m in re.finditer(pattern, text, re.I):
            s = text[max(0, m.start() - _WINDOW): m.end() + _WINDOW].strip()
            key = s[:80]
            if key not in seen:
                seen.add(key)
                snippets.append("…" + s + "…")
            if len(snippets) >= _MAX_SNIPPETS:
                break
        if snippets:
            out[term] = snippets
    return out


def status_from_evidence(evidence, scanned=True):
    if not scanned:
        return STATUS["UNSCANNED"]
    if any(evidence.get(t) for t in DECANT_TERMS):
        return STATUS["DECANT"]
    if any(evidence.get(t) for t in ACCOMMODATION_TERMS):
        return STATUS["ACCOM"]
    if any(evidence.get(t) for t in OCCUPANCY_RISK_TERMS):
        return STATUS["RISK"]
    return STATUS["NONE"]


def extract_deadlines(evidence):
    dates, seen = [], set()
    for snippets in evidence.values():
        for s in snippets:
            for m in _DATE_RE.finditer(s):
                d = m.group(1)
                if d.lower() not in seen:
                    seen.add(d.lower())
                    dates.append(d)
    return dates[:8]


# ---- baseline sales priorities (TheSqua.re BSA decant report, 10 Jul 2026) ----
PRIORITY_RULES = [
    ("Oyster Bay", "Medium-High"), ("Brayford", "Very High"), ("Chepstow Villas", "Low"),
    ("Hallings Wharf", "High"), ("Marconi House", "Low-Medium"), ("Ridley House", "Medium"),
    ("Pieris House", "Medium"), ("Somerville Court", "High"), ("Leigham Court Road", "Medium-High"),
    ("Kaleidscope", "High"), ("East Village", "Medium"),
    ("Canary Riverside", "High", "Remediation order"),
    ("Canary Riverside", "Medium", "Accountable Person"),
    ("Canary Riverside", "Medium-High"),
    ("Cypress Point", "Medium"), ("Enterprise Rent", "Low"), ("Bracken House", "Medium-High"),
    ("Millroyd Mill", "Medium"), ("Wotton Court", "Medium-High"), ("Burstock", "Low-Medium"),
    ("Laurels", "Low-Medium"), ("2 Hillside", "Medium"), ("Planetree", "Medium"),
    ("Globe View", "Low-Medium"), ("Navigation Court", "Low-Medium"), ("Monument Court", "Low-Medium"),
    ("St Anne's Quay", "Low"), ("Centrillion", "Medium-High"), ("Empire Square", "Very High"),
    ("Purbeck House", "Medium-High"), ("Prince of Wales Road", "Medium-High"),
    ("Focus Apartments", "Medium"), ("Thanet Lodge", "Medium"), ("Iverson Road", "Medium"),
    ("Vista Tower", "High"), ("Praed Street", "Medium-High"), ("Artillery Row", "Medium-High"),
    ("Remus Road", "Medium-High"), ("Chocolate Box", "Medium"), ("Space Apartments", "Medium"),
    ("Spur House", "Medium"), ("Ovington Court", "Low-Medium"), ("Orchard House", "Medium"),
    ("Sutton Court Road", "Medium"), ("Grove House", "Low"),
]
_BASELINE_SRC = "TheSqua.re BSA decant report, 10 Jul 2026"


def match_priority(title, sub_category):
    t = (title or "").lower()
    sc = (sub_category or "").lower()
    for rule in PRIORITY_RULES:
        frag, prio = rule[0], rule[1]
        sub = rule[2] if len(rule) > 2 else None
        if frag.lower() not in t:
            continue
        if sub and sub.lower() not in sc:
            continue
        return {"value": prio, "source": _BASELINE_SRC}
    return {"value": "Unrated", "source": "new case — not in baseline report"}


_SUBCAT_MAP = {
    "remediation-order": "Remediation order",
    "remediation-contribution-order": "Remediation contribution order",
    "accountable-person": "Accountable Person",
    "building-act-1984-and-bsa-regs": "Building Act 1984 and BSA Regs",
    "landlord-appeal-against-liability-to-pay-a-remediation-amount":
        "Landlord appeal against liability to pay a remediation amount",
    "failure-to-provide-documents-or-information": "Failure to provide documents or information",
    "remediation-order-main-decision": "Remediation order / main decision",
}


def pretty_sub(s):
    s = str(s or "").replace("Building Safety Act - ", "").strip()
    k = re.sub(r"^-+", "", re.sub(r"^building-safety-act-*", "", s))
    if k in _SUBCAT_MAP:
        return _SUBCAT_MAP[k]
    if re.fullmatch(r"[a-z0-9-]+", s):
        t = re.sub(r"-+", " ", k).strip()
        return t[:1].upper() + t[1:]
    return s
