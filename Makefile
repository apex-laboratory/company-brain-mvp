# Company Brain — developer tasks.
# Usage: `make docs`, `make test`, `make help`

PY := brain-api/.venv/bin/python

.DEFAULT_GOAL := help

.PHONY: docs test help

docs: ## Regenerate docs/openapi.json + docs/api.html from the live app
	$(PY) brain-api/scripts/gen_api_docs.py

test: ## Run the backend test suite
	cd brain-api && .venv/bin/pytest app/tests -q

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
