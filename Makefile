# Convenience targets. Everything also works as plain `uv run ...` commands.
.PHONY: setup sandbox test check run run-v2 batch serve lint

setup:            ## one-command setup: Python deps + browser
	uv sync && uv run playwright install chromium && (test -f .env || (cp .env.example .env && chmod 600 .env && echo "Created .env: fill it in"))

sandbox:          ## create the private test repo in YOUR GitHub account + inputs/local_*.json
	uv run python scripts/create_sandbox.py --yes

test:             ## offline test suite
	uv run pytest -q

check:            ## resolver: every saved page x every mutation, offline
	uv run bravebird-offline check

lint:
	uv run ruff check src tests scripts

run:              ## one run with inputs/github_v1.json
	uv run bravebird --input inputs/github_v1.json

run-v2:           ## same code, different document
	uv run bravebird --input inputs/github_v2.json

batch:            ## both inputs at once
	uv run bravebird-batch inputs/github_v1.json inputs/github_v2.json --concurrency 2

serve:            ## job queue + API + web UI on http://127.0.0.1:8765
	uv run bravebird-serve
