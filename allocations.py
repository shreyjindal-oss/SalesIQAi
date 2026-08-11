"""
Lead allocation — assign a lead to a salesperson, and email them when that
lead's data changes on a later crawl.

- Active salespersons (name -> email) come from the admin enquiry app API and are
  cached in Datastore ("salespersons").
- Allocations are stored in Datastore ("allocations") keyed by "<board>::<lead_id>",
  each carrying a content signature of the lead at assignment/last-notify time.
- After every crawl, notify_updates() re-signatures each allocated lead and emails
  the assignee if it changed.
"""
import hashlib
import json
from datetime import datetime, timezone

import requests

import store

SALESPERSONS_API = ("https://admin-enquiry-app-219724630519.us-central1.run.app"
                    "/admin/api_get_active_salespersons")

# board -> (dataset name, list key inside the doc, id field on each lead)
_BOARDS = {
    "decant": ("cases", "cases", "slug"),
    "floods": ("floods", "leads", "id"),
    "tenders": ("tenders", "leads", "id"),
    "infra": ("corp_infra", "leads", "id"),
    "prospects": ("prospects", "leads", "id"),
    "hq": ("hq", "projects", "id"),
    "uk": ("ukmoves", "projects", "id"),
}
_VOLATILE = {"is_new", "last_changed", "first_seen", "changed_fields", "generated_at"}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- salespersons ----------------------------------------------------------
def fetch_salespersons():
    r = requests.get(SALESPERSONS_API, timeout=30, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    people = sorted(({"name": k, "email": v} for k, v in data.items() if v),
                    key=lambda p: p["name"].lower())
    store.put_json("salespersons", {"people": people, "updated": _now()})
    return people


def get_salespersons(refresh=False):
    doc = store.get_json("salespersons")
    if refresh or not doc or not doc.get("people"):
        try:
            return fetch_salespersons()
        except Exception:
            return (doc or {}).get("people", [])
    return doc["people"]


# ---- allocations -----------------------------------------------------------
def get_allocations():
    return store.get_json("allocations", {}) or {}


def _find_lead(board, lead_id):
    spec = _BOARDS.get(board)
    if not spec:
        return None
    dataset, list_key, id_field = spec
    doc = store.get_json(dataset) or {}
    for lead in doc.get(list_key, []):
        if str(lead.get(id_field)) == str(lead_id):
            return lead
    return None


def _signature(lead):
    if not lead:
        return ""
    slim = {k: v for k, v in lead.items() if k not in _VOLATILE}
    # collapse the decant sub-object to just its status (the meaningful change)
    if isinstance(slim.get("decant"), dict):
        slim["decant"] = slim["decant"].get("status")
    return hashlib.sha256(json.dumps(slim, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def allocate(board, lead_id, title, name, email):
    if board not in _BOARDS:
        raise ValueError("unknown board: %s" % board)
    allocs = get_allocations()
    key = board + "::" + str(lead_id)
    allocs[key] = {
        "board": board, "lead_id": str(lead_id), "title": title or "",
        "name": name or "", "email": email or "", "allocated_at": _now(),
        "signature": _signature(_find_lead(board, lead_id)), "last_notified": "",
    }
    store.put_json("allocations", allocs)
    return allocs[key]


def unallocate(board, lead_id):
    allocs = get_allocations()
    allocs.pop(board + "::" + str(lead_id), None)
    store.put_json("allocations", allocs)


def public_map():
    """Compact map the dashboard uses: key -> {name, email}."""
    return {k: {"name": a.get("name"), "email": a.get("email")} for k, a in get_allocations().items()}


# ---- update notifications (called at the end of each crawl) ----------------
def notify_updates():
    import emailer  # local import to avoid a cycle
    allocs = get_allocations()
    if not allocs:
        return {"checked": 0, "notified": 0}
    notified, changed = 0, False
    for a in allocs.values():
        lead = _find_lead(a["board"], a["lead_id"])
        if not lead:
            continue  # lead absent this run — keep allocation, no alert
        sig = _signature(lead)
        if sig != a.get("signature"):
            result = emailer.send_lead_update(a["email"], a["name"], a["board"], lead)
            a["signature"] = sig
            a["last_notified"] = _now()
            changed = True
            if result == "sent":
                notified += 1
    if changed:
        store.put_json("allocations", allocs)
    return {"checked": len(allocs), "notified": notified}
