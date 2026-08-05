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
