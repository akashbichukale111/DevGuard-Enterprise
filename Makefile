# DevGuard AI — common tasks.
#
# Every target here is something a reviewer might actually want to run, and
# every one of them works without a Groq API key, without a collector, and
# without SigNoz. Targets that need something unavailable say so rather than
# failing obscurely.

PYTHON ?= python
PORT   ?= 8000

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@echo ""
	@echo "DevGuard AI — make targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

.PHONY: doctor
doctor:  ## Preflight: what is installed, what is missing, what to do about it
	@$(PYTHON) scripts/doctor.py

.PHONY: install
install:  ## Install backend and frontend dependencies
	$(PYTHON) -m pip install -r requirements.txt
	cd frontend && npm install

.PHONY: test
test:  ## Run the test suite (no API key or network required)
	$(PYTHON) -m pytest

.PHONY: verify-otel
verify-otel:  ## Prove OTLP export + trace context + log correlation are real
	$(PYTHON) scripts/verify_otel.py

.PHONY: eval
eval:  ## Fault-injection evaluation suite. Needs the substrate Postgres.
	$(PYTHON) scripts/run_eval.py
	$(PYTHON) scripts/render_eval_readme.py
	@echo ""
	@echo "eval: results in examples/eval/ (README.md is generated from results.json)"

.PHONY: ablation
ablation:  ## Retrieval ablation. Needs the substrate, DataHub and a token.
	$(PYTHON) scripts/run_ablation.py -n 5
	$(PYTHON) scripts/render_ablation_readme.py

.PHONY: scan-secrets
scan-secrets:  ## Secret scan over every tracked file
	$(PYTHON) scripts/scan_secrets.py

.PHONY: replay
replay:  ## Build replay bundles from the committed proof packs (05 the zero-infrastructure replay requirement)
	$(PYTHON) scripts/build_replay.py
	@echo ""
	@echo "replay: bundles in frontend/public/replay/. 'make replay-serve' to view them."

# The whole point of the zero-infrastructure replay requirement is that this needs nothing: no DataHub, no LLM key,
# no Postgres, no backend. It is a static export served as files.
.PHONY: replay-build
replay-build: replay  ## Static export of the Command Center — zero infrastructure
	cd frontend && NEXT_OUTPUT=export npm run build
	@echo ""
	@echo "replay-build: static site in frontend/out/ — open /command/"

.PHONY: replay-serve
replay-serve: replay-build  ## Serve the static replay UI on PORT (default 8000)
	@echo "Command Center: http://localhost:$(PORT)/command/"
	cd frontend/out && $(PYTHON) -m http.server $(PORT)

# Not part of `verify`: it needs a browser binary, and pinning playwright as a
# devDependency would put a browser download into every CI install for a check
# CI does not run. Installed on demand here instead.
.PHONY: verify-replay-ui
verify-replay-ui: replay-build  ## Drive the built replay UI in a browser and assert the zero-infrastructure replay requirement
	cd frontend && npm install --no-save --no-audit --no-fund playwright
	cd frontend/out && $(PYTHON) -m http.server 8931 & echo $$! > /tmp/devguard-replay.pid
	@sleep 2
	cd frontend && node scripts/verify_replay_ui.mjs; \
	  status=$$?; kill `cat /tmp/devguard-replay.pid` 2>/dev/null; \
	  rm -f /tmp/devguard-replay.pid; exit $$status

.PHONY: verify
verify: test scan-secrets verify-otel lint build  ## Everything CI runs, locally
	@echo ""
	@echo "verify: all checks passed."

.PHONY: lint
lint:  ## Lint the frontend
	cd frontend && npm run lint

.PHONY: build
build:  ## Type-check and build the frontend
	cd frontend && npm run build

.PHONY: backend
backend:  ## Run the backend (works with no API key; /scan needs one)
	$(PYTHON) -m uvicorn backend.main:app --reload --port $(PORT)

.PHONY: frontend
frontend:  ## Run the frontend dev server
	cd frontend && npm run dev

.PHONY: clean
clean:  ## Remove build artifacts and caches
	rm -rf frontend/.next frontend/tsconfig.tsbuildinfo
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	rm -rf .pytest_cache
