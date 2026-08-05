"""
Datastore (NDB) persistence — one JSON document per dataset.

Each crawled board (cases, floods, tenders, corp_infra, prospects, hq, ukmoves,
roster, changelog, ut_seen) is stored as a single Dataset entity keyed by name,
holding a JSON blob. This mirrors a simple key/value document store and keeps the
crawl logic identical to the source implementation.

NDB needs a client context per request/task; use `with store.context():` around
any get/put. On Cloud Run, Application Default Credentials + GOOGLE_CLOUD_PROJECT
are picked up automatically.
"""
import json
import os

from google.cloud import ndb

_client = ndb.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)


def context():
    """Context manager that binds an NDB client context to the current thread."""
    return _client.context()


class Dataset(ndb.Model):
    """A single named JSON document."""
    data = ndb.TextProperty(compressed=True)
    updated = ndb.DateTimeProperty(auto_now=True)


def get_json(name, default=None):
    ent = Dataset.get_by_id(name)
    if not ent or not ent.data:
        return default
    try:
        return json.loads(ent.data)
    except (ValueError, TypeError):
        return default


def put_json(name, obj):
    Dataset(id=name, data=json.dumps(obj, ensure_ascii=False)).put()
