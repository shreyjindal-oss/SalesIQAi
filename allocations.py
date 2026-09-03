"""
Lead workflow — assign a lead to a salesperson, move it through pipeline stages,
log time-stamped comments (a chatter feed), and email the assignee when the lead's
underlying data changes on a later crawl.

- Active salespersons (name -> email) come from the admin enquiry app API and are
  cached in Datastore ("salespersons").
- One tracking record per interacted lead lives in Datastore ("allocations"), keyed
  by "<board>::<lead_id>": assignee, pipeline stage, a comments list, and a content
  signature of the lead at assignment / last-notify time.
- After every crawl, notify_updates() re-signatures each assigned lead and emails
  the assignee if it changed.
"""
import hashlib
import json
from datetime import datetime, timezone

import requests

import store

SALESPERSONS_API = ("https://admin-enquiry-app-219724630519.us-central1.run.app"
                    "/admin/api_get_active_salespersons")

# Pipeline stages, in order. "New" = surfaced but not worked (unassigned/untouched).
STAGES = ["New", "Assigned", "Qualified", "Contacted", "Proposal",
          "Negotiation", "Won", "Lost", "On hold"]

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


def _new_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _migrate(rec):
    """Old records stored free-text 'comments'; promote them to follow-up items."""
    if rec.get("followups") is None:
        rec["followups"] = [
            {"id": _new_id(), "ts": c.get("ts", ""), "by": c.get("by", ""),
             "text": c.get("text", ""), "due": "", "done": False, "done_ts": ""}
            for c in (rec.get("comments") or [])
        ]
        rec.pop("comments", None)
    return rec


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


def _get_or_create(allocs, board, lead_id, title):
    if board not in _BOARDS:
        raise ValueError("unknown board: %s" % board)
    key = board + "::" + str(lead_id)
    rec = allocs.get(key)
    if not rec:
        rec = {"board": board, "lead_id": str(lead_id), "title": title or "",
               "name": "", "email": "", "stage": "New", "stage_by": "", "stage_at": "",
               "allocated_at": "", "followups": [], "signature": "", "last_notified": "",
               "updated": _now()}
        allocs[key] = rec
    _migrate(rec)
    if title and not rec.get("title"):
        rec["title"] = title
    return rec


def allocate(board, lead_id, title, name, email, by=""):
    allocs = get_allocations()
    rec = _get_or_create(allocs, board, lead_id, title)
    rec.update(name=name or "", email=email or "", allocated_at=_now(), updated=_now())
    rec["signature"] = _signature(_find_lead(board, lead_id))
    rec["last_notified"] = ""
    if rec.get("stage") in (None, "", "New"):
        rec["stage"] = "Assigned"
        rec["stage_by"] = by or name or ""
        rec["stage_at"] = _now()
    store.put_json("allocations", allocs)
    return rec


def unallocate(board, lead_id):
    allocs = get_allocations()
    key = board + "::" + str(lead_id)
    rec = allocs.get(key)
    if not rec:
        return
    rec.update(name="", email="", updated=_now())
    if rec.get("stage") == "Assigned":
        rec["stage"] = "New"
    # Drop the record entirely if nothing meaningful is left to keep.
    if rec.get("stage") == "New" and not rec.get("followups"):
        allocs.pop(key, None)
    store.put_json("allocations", allocs)


def set_stage(board, lead_id, title, stage, by=""):
    if stage not in STAGES:
        raise ValueError("unknown stage: %s" % stage)
    allocs = get_allocations()
    rec = _get_or_create(allocs, board, lead_id, title)
    rec.update(stage=stage, stage_by=by or "", stage_at=_now(), updated=_now())
    if rec.get("email") and not rec.get("signature"):
        rec["signature"] = _signature(_find_lead(board, lead_id))
    store.put_json("allocations", allocs)
    return rec


def add_followup(board, lead_id, title, text, by="", due=""):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty follow-up")
    allocs = get_allocations()
    rec = _get_or_create(allocs, board, lead_id, title)
    item = {"id": _new_id(), "ts": _now(), "by": by or "", "text": text[:2000],
            "due": (due or "")[:10], "done": False, "done_ts": ""}
    rec.setdefault("followups", []).append(item)
    rec["updated"] = _now()
    store.put_json("allocations", allocs)
    return item


def toggle_followup(board, lead_id, item_id, done, by=""):
    allocs = get_allocations()
    key = board + "::" + str(lead_id)
    rec = allocs.get(key)
    if not rec:
        raise ValueError("no such lead")
    _migrate(rec)
    found = None
    for it in rec.get("followups", []):
        if str(it.get("id")) == str(item_id):
            it["done"] = bool(done)
            it["done_ts"] = _now() if done else ""
            if done and by:
                it["done_by"] = by
            found = it
            break
    if not found:
        raise ValueError("no such follow-up")
    rec["updated"] = _now()
    store.put_json("allocations", allocs)
    return found


def public_map():
    """Compact map: key -> {name, email} (assignee only)."""
    return {k: {"name": a.get("name"), "email": a.get("email")}
            for k, a in get_allocations().items() if a.get("email")}


def public_tracking():
    """Full per-lead workflow records for the dashboard (no internal signature)."""
    out = {}
    for k, a in get_allocations().items():
        _migrate(a)
        out[k] = {"board": a.get("board"), "lead_id": a.get("lead_id"),
                  "title": a.get("title", ""), "name": a.get("name", ""),
                  "email": a.get("email", ""), "stage": a.get("stage", "New"),
                  "stage_by": a.get("stage_by", ""), "stage_at": a.get("stage_at", ""),
                  "allocated_at": a.get("allocated_at", ""), "updated": a.get("updated", ""),
                  "followups": a.get("followups", [])}
    return out


# ---- update notifications (called at the end of each crawl) ----------------
def notify_updates():
    import emailer  # local import to avoid a cycle
    allocs = get_allocations()
    if not allocs:
        return {"checked": 0, "notified": 0}
    notified, changed = 0, False
    for a in allocs.values():
        if not a.get("email"):
            continue  # unassigned (stage/comments only) — nobody to notify
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
