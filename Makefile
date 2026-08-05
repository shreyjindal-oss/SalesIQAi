# Developer shortcuts. Override REGION/SERVICE/PROJECT on the command line, e.g.
#   make deploy PROJECT=my-proj REGION=europe-west2
SERVICE ?= sales-intelligence-iq
REGION  ?= europe-west2
PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)

.PHONY: install test lint run docker deploy crawl clean

install:            ## install Python deps
	pip install -r requirements.txt

test:               ## offline logic checks (no GCP needed)
	python test_local.py

lint:               ## byte-compile all modules
	python -m py_compile *.py

run:                ## run locally (needs Datastore ADC or emulator)
	python main.py

docker:             ## build the container locally
	docker build -t $(SERVICE):dev .

deploy:             ## build + deploy to Cloud Run from source
	gcloud run deploy $(SERVICE) --source . --region $(REGION) --project $(PROJECT)

crawl:              ## trigger a crawl on the deployed service (needs CRAWL_TOKEN + URL)
	@test -n "$(URL)" || (echo "set URL=https://<run-url>"; exit 1)
	curl -s -X POST "$(URL)/tasks/crawl" -H "X-Crawl-Token=$(CRAWL_TOKEN)" | head -c 800

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
