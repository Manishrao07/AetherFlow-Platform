.PHONY: help up down logs migrate seed test lint format typecheck check docker-test up-observability load-test ui demo diagrams

help:            ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

up:              ## Build and start everything (API :8000, UI :8501)
	docker compose up --build

down:            ## Stop and remove containers (keeps the database volume)
	docker compose down

up-observability: ## Start everything plus Prometheus (:9090) and Grafana (:3000)
	docker compose --profile observability up --build

load-test:       ## Load-test a running API (start it with RATE_LIMIT_ENABLED=false)
	python scripts/load_test.py --base-url http://localhost:8000

demo:            ## Guided demo against the running stack (run `make seed` first)
	docker compose run --rm -e API_BASE_URL=http://api:8000 api python scripts/demo.py

ui:              ## Run the Streamlit UI locally against API_BASE_URL (default http://localhost:8000)
	streamlit run ui/streamlit_app.py

diagrams:        ## Re-export docs/diagrams/*.md to SVG/PNG (needs Node + Chromium)
	python scripts/export_diagrams.py

logs:            ## Follow API logs
	docker compose logs -f api

migrate:         ## Apply database migrations inside Docker
	docker compose run --rm migrate

seed:            ## Create demo users (one per role) and sample documents
	docker compose run --rm api python scripts/seed.py

test:            ## Run the test-suite locally (needs Postgres, see README)
	pytest

docker-test:     ## Run the test-suite fully inside Docker
	docker compose --profile test run --rm tests

lint:            ## Lint + format check
	ruff check . && ruff format --check .

format:          ## Auto-format and fix lint issues
	ruff check --fix . && ruff format .

typecheck:       ## Static type check
	mypy

check: lint typecheck test   ## Everything CI runs (minus docker build)
