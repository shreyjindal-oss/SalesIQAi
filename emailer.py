"""
Daily digest email via SendGrid.

Leads with a cross-board 'new accommodation signals' summary (EXPLICIT /
MOBILISATION / HINT, each with a rough scale estimate and link), followed by the
tribunal / floods / tenders detail. Scale figures are explicit estimates.
"""
import html

import requests

from config import CONFIG
from analyzer import STATUS

SEND_URL = "https://api.sendgrid.com/v3/mail/send"


def _esc(t):
    return html.escape(str(t), quote=False)


def _size_val(v):
    if v is None:
        return "scale not stated"
    if v < 1_000_000:
        return "handful of staff on site"
    if v < 10_000_000:
        return "~5–25 people on site"
    if v < 50_000_000:
        return "~25–100 people on site"
    if v < 250_000_000:
        return "~100–400 people on site"
    return "400+ people on site"


def _size_flood(sev):
    return {1: "potential mass displacement", 2: "tens of households at risk"}.get(sev, "early watch")


def _lv_tag(level):
    color = {"EXPLICIT": "#d64560", "MOBILISATION": "#e08a1e"}.get(level, "#2b8ad6")
    return ('<span style="background:%s;color:#fff;border-radius:10px;padding:1px 7px;'
            'font-size:11px">%s</span>' % (color, level))


def _digest_items(data, alerts, floods, tenders, corp):
    items = []

    def by_id(arr):
        return {l["id"]: l for l in (arr or [])}

    for c in (alerts["newCases"] + alerts["newSignals"]):
        items.append({"board": "Decant housing", "title": c["title"], "level": "EXPLICIT",
                      "size": "scale not stated", "url": c["url"]})
    fb = by_id((floods or {}).get("leads"))
    for fid in (floods or {}).get("new", []):
        l = fb.get(fid)
        if l:
            items.append({"board": "Floods", "title": l["area"],
                          "level": "HINT" if l["severity_level"] == 3 else "MOBILISATION",
                          "size": _size_flood(l["severity_level"]),
                          "url": "https://check-for-flooding.service.gov.uk/"})
    tb = by_id(tenders["tenders"].get("leads"))
    for tid in tenders["tenders"].get("new", []):
        l = tb.get(tid)
        if l:
            items.append({"board": "Govt tenders", "title": l["title"], "level": "EXPLICIT",
                          "size": _size_val(l.get("value_amount")), "url": l["url"]})
    ib = by_id(tenders["infra"].get("leads"))
    for iid in tenders["infra"].get("new", []):
        l = ib.get(iid)
        if l:
            items.append({"board": "UK infra wins", "title": l["title"], "level": "MOBILISATION",
                          "size": _size_val(l.get("value_amount")), "url": l["url"]})
    pb = by_id(tenders["prospects"].get("leads"))
    for pid in tenders["prospects"].get("new", []):
        l = pb.get(pid)
        if l:
            items.append({"board": "Prospect triggers", "title": l["account"] + " — " + l["contract_title"],
                          "level": "MOBILISATION", "size": _size_val(l.get("value_amount")), "url": l["url"]})
    for bn, src in (("London", (corp or {}).get("hq")), ("UK ex-London", (corp or {}).get("uk"))):
        for x in [p for p in (src or {}).get("projects", []) if p.get("is_new")]:
            items.append({"board": bn + " projects", "title": x["title"], "level": "MOBILISATION",
                          "size": _size_val(x.get("value_amount")), "url": x["url"]})
        for x in [n for n in (src or {}).get("news", []) if n.get("is_new")]:
            items.append({"board": bn + " news", "title": x["title"], "level": "HINT",
                          "size": "news lead", "url": x["link"]})
    rank = {"EXPLICIT": 0, "MOBILISATION": 1, "HINT": 2}
    items.sort(key=lambda i: rank[i["level"]])
    return items


def build_html(data, alerts, floods, tenders, corp):
    items = _digest_items(data, alerts, floods, tenders, corp)
    n_exp = sum(1 for i in items if i["level"] == "EXPLICIT")
    n_mob = sum(1 for i in items if i["level"] == "MOBILISATION")
    n_hint = sum(1 for i in items if i["level"] == "HINT")
    dash = ('<p><a href="%s" style="color:#2757c4">Open the dashboard →</a></p>' % CONFIG["DASHBOARD_URL"]
            if CONFIG["DASHBOARD_URL"] else "")
    head = ('<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px;margin:auto;color:#1c2333">'
            '<h2 style="margin-bottom:2px">🏠 New accommodation signals</h2>'
            '<p style="color:#667;margin-top:2px">%s</p>%s'
            % (("<b>%d</b> new since the last crawl · %d explicit · %d mobilisation · %d hint(s) "
                "<span style=\"color:#99a\">· scale figures are rough estimates — verify.</span>"
                % (len(items), n_exp, n_mob, n_hint)) if items
               else "No new accommodation signals since the last crawl.", dash))
    body = "".join(
        '<div style="border-left:3px solid #6cc7ff;padding:7px 12px;margin:6px 0;background:#f5f8fc">'
        '%s <b>%s</b><br><span style="color:#555;font-size:13px">%s · est. scale: %s</span> · '
        '<a href="%s" style="color:#2757c4;font-size:13px">view</a></div>'
        % (_lv_tag(i["level"]), _esc(i["title"][:110]), _esc(i["board"]), _esc(i["size"]), i["url"])
        for i in items[:25])
    tail = ('<h3 style="margin:22px 0 6px">Portfolio</h3>'
            '<p style="font-size:14px;color:#333">%d tribunal cases tracked · %d floods · '
            '%d accommodation tenders · %d infra wins · %d prospect matches</p></div>'
            % (data["case_count"], floods.get("count", 0), tenders["tenders"].get("count", 0),
               tenders["infra"].get("count", 0), tenders["prospects"].get("count", 0)))
    return head + body + tail


_BOARD_LABELS = {
    "decant": "Decant housing", "floods": "Floods", "tenders": "Govt housing tenders",
    "infra": "UK infrastructure win", "prospects": "Prospect trigger",
    "hq": "London move", "uk": "UK ex-London move",
}


def _lead_title(board, lead):
    if board == "decant":
        return lead.get("title") or lead.get("case") or lead.get("slug") or "case"
    if board == "floods":
        return lead.get("area") or "flood area"
    if board == "prospects":
        return (lead.get("account") or "") + " — " + (lead.get("contract_title") or lead.get("title") or "")
    return lead.get("title") or lead.get("account") or "lead"


def _lead_url(board, lead):
    return (lead.get("url") or lead.get("link")
            or (CONFIG["DASHBOARD_URL"] or "") or "https://check-for-flooding.service.gov.uk/")


def build_update_html(name, board, lead):
    label = _BOARD_LABELS.get(board, board)
    title = _lead_title(board, lead)
    url = _lead_url(board, lead)
    changed = lead.get("changed_fields") or []
    changed_html = ("<p style=\"color:#555;font-size:13px\">Changed: %s</p>"
                    % _esc(", ".join(changed)) if changed else "")
    dash = ('<p><a href="%s" style="color:#2757c4">Open the dashboard →</a></p>' % CONFIG["DASHBOARD_URL"]
            if CONFIG["DASHBOARD_URL"] else "")
    return ('<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px;margin:auto;color:#1c2333">'
            '<h2 style="margin-bottom:2px">🔔 Update on a lead assigned to you</h2>'
            '<p style="color:#667;margin-top:2px">Hi %s, a lead you own has new data.</p>'
            '<div style="border-left:3px solid #e08a1e;padding:8px 12px;margin:8px 0;background:#fff7ec">'
            '<span style="color:#888;font-size:12px">%s</span><br><b>%s</b><br>%s'
            '<a href="%s" style="color:#2757c4;font-size:13px">view lead →</a></div>%s</div>'
            % (_esc(name or "there"), _esc(label), _esc(title[:140]), changed_html, url, dash))


def send_lead_update(email, name, board, lead):
    """Email the assignee that their allocated lead changed. Returns a status string."""
    if not CONFIG["SENDGRID_API_KEY"] or not email:
        return "skipped: no SendGrid key / recipient"
    subject = "Sales IQ: update on your lead — %s" % _lead_title(board, lead)[:80]
    payload = {
        "personalizations": [{"to": [{"email": email.strip()}]}],
        "from": {"email": CONFIG["EMAIL_FROM"]},
        "subject": subject,
        "content": [{"type": "text/html", "value": build_update_html(name, board, lead)}],
    }
    try:
        res = requests.post(SEND_URL, json=payload, timeout=30,
                            headers={"Authorization": "Bearer " + CONFIG["SENDGRID_API_KEY"],
                                     "Content-Type": "application/json"})
        return "sent" if res.status_code in (200, 201, 202) else "failed HTTP %s: %s" % (res.status_code, res.text[:200])
    except Exception as e:
        return "failed: " + str(e)


def send_digest(data, alerts, floods, tenders, corp):
    if not CONFIG["SENDGRID_API_KEY"] or not CONFIG["EMAIL_TO"]:
        return "skipped: no SendGrid key / recipient"
    items = _digest_items(data, alerts, floods, tenders, corp)
    subject = ("Sales IQ: %d new accommodation signal(s)" % len(items) if items
               else "Sales IQ daily brief — %d cases tracked" % data["case_count"])
    payload = {
        "personalizations": [{"to": [{"email": e.strip()} for e in CONFIG["EMAIL_TO"].split(",") if e.strip()]}],
        "from": {"email": CONFIG["EMAIL_FROM"]},
        "subject": subject,
        "content": [{"type": "text/html", "value": build_html(data, alerts, floods, tenders, corp)}],
    }
    try:
        res = requests.post(SEND_URL, json=payload, timeout=30,
                            headers={"Authorization": "Bearer " + CONFIG["SENDGRID_API_KEY"],
                                     "Content-Type": "application/json"})
        return "sent" if res.status_code in (200, 201, 202) else "failed HTTP %s: %s" % (res.status_code, res.text[:200])
    except Exception as e:
        return "failed: " + str(e)
