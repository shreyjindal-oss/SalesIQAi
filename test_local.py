"""Offline unit checks for the ported builders/parsers (no network, no NDB)."""
import sys
import types
import contextlib

# Stub the NDB-backed store with an in-memory dict so crawler imports cleanly.
_mem = {}
_store = types.ModuleType("store")
_store.get_json = lambda n, d=None: _mem.get(n, d)
_store.put_json = lambda n, o: _mem.__setitem__(n, o)
_store.context = lambda: contextlib.nullcontext()
sys.modules["store"] = _store

import crawler  # noqa: E402

fails = 0


def check(cond, label):
    global fails
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        fails += 1


print("Floods")
fl = crawler.build_flood_lead({"floodAreaID": "X1", "severityLevel": 2, "severity": "Flood Warning",
                               "description": "River Aire at Leeds", "message": "Flooding is expected",
                               "floodArea": {"county": "West Yorkshire", "riverOrSea": "River Aire"}}, None, "T")
check(fl["severity_level"] == 2 and fl["area"] == "River Aire at Leeds" and fl["is_new"], "flood lead built")

print("Tenders")
acc = {"id": "t1", "tag": ["tender"], "date": "2026-08-01",
       "tender": {"title": "Provision of temporary accommodation for homeless households",
                  "description": "supported accommodation framework", "status": "active"},
       "parties": [{"roles": ["buyer"], "name": "A Council", "address": {"locality": "Leeds"}}]}
non = {"id": "t2", "tag": ["tender"], "tender": {"title": "Payroll software", "description": "HR system"},
       "parties": [{"roles": ["buyer"], "name": "B Council"}]}
lt = crawler.build_tender_lead(acc, None, "T")
check(lt and "temporary accommodation" in lt["matched"]["keywords"], "accommodation tender matched, verbatim keyword")
check(crawler.build_tender_lead(non, None, "T") is None, "non-accommodation tender dropped")

print("Infra wins")
big = {"id": "i1", "tag": ["award", "contract"], "date": "2026-08-01",
       "parties": [{"roles": ["buyer"], "name": "National Highways", "address": {"locality": "Birmingham"}},
                   {"roles": ["supplier"], "name": "Balfour Beatty Ltd"}],
       "tender": {"title": "A66 Upgrade", "mainProcurementCategory": "works"},
       "awards": [{"items": [{"additionalClassifications": [{"scheme": "CPV", "id": "45233100"}]}]}],
       "contracts": [{"value": {"amount": 450000000, "currency": "GBP"}}]}
small = {**big, "id": "i2", "contracts": [{"value": {"amount": 50000, "currency": "GBP"}}],
         "tender": {"title": "x", "mainProcurementCategory": "services"}, "awards": []}
w = crawler.build_infra_win(big, None, "T")
check(w and w["value_amount"] == 450000000 and "Balfour Beatty Ltd" in w["suppliers"], "large works award captured")
check(crawler.build_infra_win(small, None, "T") is None, "small/non-infra award dropped")

print("London / UK projects")
lon = {"id": "L1", "tag": ["award"],
       "parties": [{"roles": ["buyer"], "name": "TfL", "address": {"region": "UKI31", "locality": "London"}},
                   {"roles": ["supplier"], "name": "Costain Ltd"}],
       "tender": {"title": "Depot works", "mainProcurementCategory": "works"},
       "contracts": [{"value": {"amount": 8000000, "currency": "GBP"}}]}
leeds = {**lon, "id": "L2", "parties": [{"roles": ["buyer"], "name": "Leeds CC", "address": {"region": "UKE42", "locality": "Leeds"}},
                                        {"roles": ["supplier"], "name": "Some Builder Ltd"}]}
check(crawler._is_london_award(lon) and not crawler._is_london_award(leeds), "London detection by UKI region")
check(crawler.build_londonproject(lon, None, "T")["value_amount"] == 8000000 if hasattr(crawler, "build_londonproject") else crawler._build_area_project(lon, None, "T", True)["value_amount"] == 8000000, "London project ≥ £1m captured")
check(crawler._build_area_project(lon, None, "T", False) is None, "London award excluded from UK board")
check(crawler._build_area_project(leeds, None, "T", False)["buyer"] == "Leeds CC", "non-London award kept for UK board")

print("Roster matching")
idx = crawler.build_roster_index([{"account": "Balfour Beatty", "owner": "Sharon"},
                                  {"account": "Morgan Sindall", "owner": "Emily"},
                                  {"account": "Tiny Co", "owner": "Sam"}])
check(crawler.match_roster(idx, "Balfour Beatty PLC")["rec"]["owner"] == "Sharon", "exact winner matched")
check(crawler.match_roster(idx, "Morgan Sindall Infrastructure Limited")["match_type"] == "partial", "multi-token partial match")
check(crawler.match_roster(idx, "Completely Different Ltd") is None, "unrelated winner not matched")

print("Roster CSV")
csv_txt = ('Account Name,Account Owner,Website\n'
           'Balfour Beatty,Sharon Baker,"<a href=""x"" title=""https://www.balfourbeatty.com/ (New Window)"">'
           'https://www.balfourbeatty.com/</a>"\nPetrofac,Sabir Potts,http://petrofac.com/\n'
           'Petrofac,Sabir Potts,http://petrofac.com/\n,Nobody,\n')
accts = crawler.parse_roster_csv(csv_txt)
check(len(accts) == 2, "CSV dedup + blank-account drop")
check(accts[0]["domain"] == "balfourbeatty.com", "domain from Salesforce anchor")

print("GNews parse")
gj = '{"articles":[{"title":"Acme opens Manchester office","url":"https://bbc.co.uk/m","publishedAt":"2026-08-03T09:00:00Z","source":{"name":"BBC","url":"https://bbc.co.uk"}},{"title":"Foo relocates London HQ","url":"https://sky.com/l","publishedAt":"2026-08-02T09:00:00Z","source":{"name":"Sky"}}]}'
allnews = crawler.parse_gnews(gj, False)
uknews = crawler.parse_gnews(gj, True)
check(len(allnews) == 2 and allnews[0]["publisher"] == "BBC", "gnews parsed + source name")
check(len(uknews) == 1 and "london" not in uknews[0]["title"].lower(), "London-titled news filtered when excludeLondon")
check(crawler.parse_gnews("Unauthorized", False) == [], "non-JSON payload handled safely")

print("Tribunal case build")
c = crawler.build_case({"link": "/x/one-brayford", "public_timestamp": "2026-06-16", "title": "One The Brayford"},
                       {"title": "One The Brayford LN1", "details": {"metadata": {
                           "tribunal_decision_sub_category": "remediation-contribution-order",
                           "tribunal_decision_decision_date": "2026-06-15",
                           "hidden_indexable_content": "a decant of the building took place; temporary accommodation"},
                           "body": "<p>owing to the prohibition notice</p>", "attachments": []}}, None, "T")
check(c["decant"]["status"] == crawler.STATUS["DECANT"], "decant status from verbatim text")
check(c["sub_category"] == "Remediation contribution order", "sub-category mapped")
check(c["priority"]["value"] == "Very High", "Brayford priority from baseline report")

print("Lead allocation")
_mem.clear()
import allocations  # noqa: E402
# stub the salespersons API fetch (no network in tests)
allocations.fetch_salespersons = lambda: (_store.put_json(
    "salespersons", {"people": [{"name": "Asha Rao", "email": "asha@thesqua.re"}]}) or
    [{"name": "Asha Rao", "email": "asha@thesqua.re"}])
_store.put_json("tenders", {"leads": [{"id": "t1", "title": "Temp accommodation", "value_amount": 100}]})
rec = allocations.allocate("tenders", "t1", "Temp accommodation", "Asha Rao", "asha@thesqua.re")
check(rec["email"] == "asha@thesqua.re" and rec["signature"], "allocation stored with a signature")
check(allocations.public_map()["tenders::t1"]["name"] == "Asha Rao", "public_map exposes assignee")

_sent = []
_em = types.ModuleType("emailer")
_em.send_lead_update = lambda email, name, board, lead: (_sent.append((email, board, lead.get("id"))) or "sent")
sys.modules["emailer"] = _em
check(allocations.notify_updates()["notified"] == 0, "no notify when lead unchanged")
_store.put_json("tenders", {"leads": [{"id": "t1", "title": "Temp accommodation", "value_amount": 250}]})
r = allocations.notify_updates()
check(r["notified"] == 1 and _sent and _sent[-1][0] == "asha@thesqua.re", "assignee notified on lead change")
check(allocations.notify_updates()["notified"] == 0, "signature updated → no repeat notify")

print("Stages + comments")
allocations.set_stage("tenders", "t1", "Temp accommodation", "Qualified", "Asha Rao")
check(allocations.get_allocations()["tenders::t1"]["stage"] == "Qualified", "stage set")
try:
    allocations.set_stage("tenders", "t1", "", "Bogus", "x")
    _bad = True
except ValueError:
    _bad = False
check(not _bad, "invalid stage rejected")
allocations.add_comment("tenders", "t1", "Temp accommodation", "Called the buyer, awaiting brief", "Asha Rao")
_c = allocations.get_allocations()["tenders::t1"]["comments"]
check(len(_c) == 1 and _c[0]["by"] == "Asha Rao" and _c[0]["ts"], "comment logged with author + timestamp")
_pt = allocations.public_tracking()["tenders::t1"]
check(_pt["stage"] == "Qualified" and _pt["comments"] and "signature" not in _pt, "public_tracking exposes stage+comments, hides signature")

# comment on an UNASSIGNED lead creates a record but never emails
_sent.clear()
allocations.add_comment("floods", "F9", "Some area", "Noting for later", "Bea Lin")
check("floods::F9" in allocations.get_allocations() and not allocations.get_allocations()["floods::F9"]["email"],
      "comment creates untracked/unassigned record")
check(allocations.notify_updates()["notified"] == 0 and not _sent, "unassigned records are never emailed")

allocations.unallocate("tenders", "t1")
check(allocations.get_allocations()["tenders::t1"]["stage"] == "Qualified", "unassign keeps a worked lead (has stage/comments)")
check(not allocations.get_allocations()["tenders::t1"]["email"], "unassign clears the assignee")
check(allocations._signature({"id": "x", "is_new": True}) == allocations._signature({"id": "x", "is_new": False}),
      "signature ignores volatile is_new flag")

print(("\nALL PASSED" if not fails else "\n%d FAILURE(S)" % fails))
sys.exit(1 if fails else 0)
