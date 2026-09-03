"""
Sales Intelligence IQ — Cloud Run web service.

Routes:
  GET  /                     dashboard (HTML, data injected from Datastore)
  GET  /api/<name>.json      raw dataset JSON (cases, floods, tenders, ...)
  POST /tasks/crawl          run the daily crawl (Cloud Scheduler target; secured)
  GET  /healthz              liveness probe

Auth for /tasks/crawl: send the shared secret as header `X-Crawl-Token` (or
`?token=`). For stronger auth, front it with Cloud Scheduler OIDC + IAM instead.
"""
import json
import os

from flask import Flask, Response, abort, jsonify, request

import store
import crawler
import allocations
from config import CONFIG

app = Flask(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_PATH = os.path.join(_HERE, "templates", "dashboard.html")

# Datasets injected into the dashboard template placeholders.
_INJECT = {
    "__FLOODS__": "floods", "__TENDERS__": "tenders", "__CORP_INFRA__": "corp_infra",
    "__PROSPECTS__": "prospects", "__HQMOVES__": "hq", "__UKMOVES__": "ukmoves",
}

_EMPTY_CASES = {"generated_at": "", "source": "#", "case_count": 0,
                "new_in_this_run": [], "updated_in_this_run": [], "cases": []}

with open(_TEMPLATE_PATH, encoding="utf-8") as _f:
    _TEMPLATE = _f.read()


def _authed():
    tok = request.headers.get("X-Crawl-Token") or request.args.get("token") or ""
    return bool(CONFIG["CRAWL_TOKEN"]) and tok == CONFIG["CRAWL_TOKEN"]


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/")
def dashboard():
    with store.context():
        cases = store.get_json("cases") or _EMPTY_CASES
        html = (_TEMPLATE
                .replace("__SOURCE__", str(cases.get("source", "#")))
                .replace("__GENERATED__", str(cases.get("generated_at", "")))
                .replace("__DATA__", json.dumps(cases, ensure_ascii=False)))
        for placeholder, name in _INJECT.items():
            doc = store.get_json(name)
            html = html.replace(placeholder, json.dumps(doc, ensure_ascii=False) if doc else "null")
        log = store.get_json("changelog", []) or []
        html = html.replace("__CHANGELOG__", json.dumps(log[-30:], ensure_ascii=False))
        people = (store.get_json("salespersons") or {}).get("people", [])
        html = html.replace("__SALESPEOPLE__", json.dumps(people, ensure_ascii=False))
        html = html.replace("__TRACKING__", json.dumps(allocations.public_tracking(), ensure_ascii=False))
        html = html.replace("__STAGES__", json.dumps(allocations.STAGES, ensure_ascii=False))
    return Response(html, mimetype="text/html")


@app.get("/api/<name>.json")
def api(name):
    allowed = {"cases", "floods", "tenders", "corp_infra", "prospects", "hq", "ukmoves",
               "roster", "changelog"}
    if name not in allowed:
        abort(404)
    with store.context():
        doc = store.get_json(name)
    return Response(json.dumps(doc if doc is not None else {}, ensure_ascii=False),
                    mimetype="application/json")


@app.get("/api/salespersons.json")
def api_salespersons():
    refresh = request.args.get("refresh") == "1"
    with store.context():
        people = allocations.get_salespersons(refresh=refresh)
    return jsonify({"people": people})


@app.get("/api/allocations.json")
def api_allocations():
    with store.context():
        return jsonify(allocations.public_map())


@app.get("/api/pipeline.json")
def api_pipeline():
    with store.context():
        return jsonify({"tracking": allocations.public_tracking(), "stages": allocations.STAGES})


@app.post("/api/allocate")
def api_allocate():
    b = request.get_json(silent=True) or request.form
    board, lead_id = b.get("board"), b.get("lead_id")
    name, email = b.get("name"), b.get("email")
    title, by = b.get("title", ""), b.get("by", "")
    if not (board and lead_id and email):
        return jsonify({"error": "board, lead_id and email are required"}), 400
    try:
        with store.context():
            rec = allocations.allocate(board, lead_id, title, name, email, by)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "record": rec})


@app.post("/api/unallocate")
def api_unallocate():
    b = request.get_json(silent=True) or request.form
    board, lead_id = b.get("board"), b.get("lead_id")
    if not (board and lead_id):
        return jsonify({"error": "board and lead_id are required"}), 400
    with store.context():
        allocations.unallocate(board, lead_id)
    return jsonify({"ok": True})


@app.post("/api/stage")
def api_stage():
    b = request.get_json(silent=True) or request.form
    board, lead_id, stage = b.get("board"), b.get("lead_id"), b.get("stage")
    title, by = b.get("title", ""), b.get("by", "")
    if not (board and lead_id and stage):
        return jsonify({"error": "board, lead_id and stage are required"}), 400
    try:
        with store.context():
            rec = allocations.set_stage(board, lead_id, title, stage, by)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "record": rec})


@app.post("/api/followup")
def api_followup():
    b = request.get_json(silent=True) or request.form
    board, lead_id, text = b.get("board"), b.get("lead_id"), b.get("text")
    title, by, due = b.get("title", ""), b.get("by", ""), b.get("due", "")
    if not (board and lead_id and (text or "").strip()):
        return jsonify({"error": "board, lead_id and text are required"}), 400
    try:
        with store.context():
            item = allocations.add_followup(board, lead_id, title, text, by, due)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "item": item})


@app.post("/api/followup/toggle")
def api_followup_toggle():
    b = request.get_json(silent=True) or request.form
    board, lead_id, item_id = b.get("board"), b.get("lead_id"), b.get("item_id")
    done, by = bool(b.get("done")), b.get("by", "")
    if not (board and lead_id and item_id):
        return jsonify({"error": "board, lead_id and item_id are required"}), 400
    try:
        with store.context():
            item = allocations.toggle_followup(board, lead_id, item_id, done, by)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "item": item})


@app.route("/tasks/crawl", methods=["POST", "GET"])
def run_crawl():
    # Cloud Scheduler OIDC sets this header; also accept the shared token.
    if not (_authed() or request.headers.get("X-CloudScheduler")):
        abort(403)
    full = request.args.get("full") == "1"
    with store.context():
        try:
            result = crawler.crawl(full=full)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
