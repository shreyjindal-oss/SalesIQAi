# Sales Intelligence IQ

Accommodation-demand intelligence for TheSqua.re — a daily crawl of official UK
open-data sources that surfaces where serviced-apartment / relocation / emergency
accommodation demand is about to appear, and a dashboard + email digest to act on it.

Every signal is filtered to an **accommodation angle** and badged by confidence
(**EXPLICIT** — the source text names accommodation; **MOBILISATION** — a contract
award means a team deploys; **HINT** — news suggests a move). Statuses derive only
from verbatim keyword matches in official text — nothing is inferred.

## Stack

- **Python 3.12 + Flask**, served by **gunicorn** on **Cloud Run**
- **Datastore (NDB)** — one JSON document per board (`google-cloud-ndb`)
- **Cloud Scheduler** → `POST /tasks/crawl` runs the daily crawl
- Frontend: a single self-contained **HTML/CSS/JS** page (`templates/dashboard.html`)
- Email digest via **SendGrid** (optional)

## Data sources (all free, open)

| Board | Source |
|-------|--------|
| Decant housing | GOV.UK Residential Property Tribunal decisions (Building Safety Act) + Content API; Upper Tribunal (Lands Chamber) via National Archives Find Case Law |
| Floods | Environment Agency flood-monitoring API |
| Govt housing tenders | Find a Tender Service (OCDS), filtered to accommodation keywords / CPV |
| UK infrastructure wins | Find a Tender awards ≥ £10m (construction / civil / rail) |
| London & UK ex-London moves | Find a Tender awards delivered in / outside London (≥ £1m) + GNews.io news-watch |
| Prospect triggers | Find a Tender award winners cross-matched to an account roster (Google Sheet CSV) |

## Layout

```
main.py          Flask app: dashboard, JSON API, /tasks/crawl, /healthz
crawler.py       full crawl pipeline (all sources) → Datastore
analyzer.py      verbatim decant-signal extraction + baseline priorities
emailer.py       SendGrid daily digest
store.py         NDB (Datastore) JSON-document store
config.py        env-var configuration
templates/dashboard.html   the frontend (data injected server-side)
Dockerfile       Cloud Run container
test_local.py    offline unit checks for the builders/parsers
```

## Configuration (env vars)

See `.env.example`. On Cloud Run, set non-secret values as service env vars and
secrets (`GNEWS_API_KEY`, `SENDGRID_API_KEY`, `CRAWL_TOKEN`) via **Secret Manager**.

| Var | Purpose |
|-----|---------|
| `GOOGLE_CLOUD_PROJECT` | Datastore project (auto on GCP) |
| `CRAWL_TOKEN` | shared secret Cloud Scheduler sends to trigger the crawl |
| `ROSTER_SHEET_URL` | Google Sheet CSV export (Account Name, Account Owner, Website) |
| `GNEWS_API_KEY` | GNews.io key for the news-watch (news empty without it) |
| `SENDGRID_API_KEY`, `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_MODE` | daily digest email |
| `DETAIL_CAP` | max GOV.UK decision detail fetches per crawl (default 40) |
| `DASHBOARD_URL` | link included in the digest email |

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python test_local.py                     # offline logic checks (no GCP needed)

# To run the app locally you need Datastore access — either real ADC:
export GOOGLE_CLOUD_PROJECT=your-project
gcloud auth application-default login
# ...or the Datastore emulator:
gcloud beta emulators datastore start &
$(gcloud beta emulators datastore env-init)

export CRAWL_TOKEN=dev-token
python main.py                           # http://localhost:8080
curl -X POST "localhost:8080/tasks/crawl?token=dev-token"   # first crawl (baseline)
```

## Deploy to Cloud Run

```bash
PROJECT=your-gcp-project
REGION=europe-west2

# 1. Enable APIs (once)
gcloud services enable run.googleapis.com datastore.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com --project $PROJECT

# 2. Datastore: create a Firestore-in-Datastore-mode database (once, if not present)
gcloud firestore databases create --location=$REGION --type=datastore-mode --project $PROJECT

# 3. Secrets
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create CRAWL_TOKEN --data-file=- --project $PROJECT
printf '%s' "$GNEWS_KEY"     | gcloud secrets create GNEWS_API_KEY --data-file=- --project $PROJECT
printf '%s' "$SENDGRID_KEY"  | gcloud secrets create SENDGRID_API_KEY --data-file=- --project $PROJECT

# 4. Build & deploy
gcloud run deploy sales-intelligence-iq \
  --source . --region $REGION --project $PROJECT \
  --allow-unauthenticated \
  --memory 512Mi --timeout 1800 \
  --set-env-vars "ROSTER_SHEET_URL=<sheet-csv-url>,EMAIL_FROM=alerts@thesqua.re,EMAIL_TO=team@thesqua.re,EMAIL_MODE=changes-only,DASHBOARD_URL=https://<run-url>" \
  --set-secrets "CRAWL_TOKEN=CRAWL_TOKEN:latest,GNEWS_API_KEY=GNEWS_API_KEY:latest,SENDGRID_API_KEY=SENDGRID_API_KEY:latest"

# 5. Daily crawl via Cloud Scheduler (07:00 UTC)
RUN_URL=$(gcloud run services describe sales-intelligence-iq --region $REGION --format 'value(status.url)')
gcloud scheduler jobs create http sales-iq-daily-crawl \
  --location $REGION --schedule "0 7 * * *" \
  --uri "$RUN_URL/tasks/crawl" --http-method POST \
  --headers "X-Crawl-Token=$(gcloud secrets versions access latest --secret CRAWL_TOKEN)"

# First run now (baseline import — no email on the baseline run):
curl -X POST "$RUN_URL/tasks/crawl" -H "X-Crawl-Token=$(gcloud secrets versions access latest --secret CRAWL_TOKEN)"
```

For a private service, drop `--allow-unauthenticated`, give the Scheduler job an
`--oidc-service-account-email`, and grant it `run.invoker`; the crawl route also
accepts Cloud Scheduler's `X-CloudScheduler` header.

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /` | dashboard |
| `GET /api/<board>.json` | `cases`, `floods`, `tenders`, `corp_infra`, `prospects`, `hq`, `ukmoves`, `roster`, `changelog` |
| `POST /tasks/crawl` | run the crawl (Scheduler target; token- or OIDC-secured) |
| `GET /healthz` | liveness |
