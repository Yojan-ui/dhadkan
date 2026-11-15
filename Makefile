# Dhadkan. `make demo` is all you need after `make setup` (and the data + models, see README).
PY      ?= .venv/bin/python
PORT    ?= 8600
WEB_SRC := $(shell find web/src web/index.html web/public -type f 2>/dev/null) web/package.json web/vite.config.ts

.PHONY: setup data train demo serve dev test lint fmt clean

setup:                       ## venv, Python and web dependencies
	python3.12 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt -r requirements-dev.txt
	cd web && npm ci

data:                        ## ~1 GB subset of MIMII + MIMII DUE (about 30 min)
	$(PY) -m data.download

train:                       ## 3 models + docs/runs/eval_<UTC>.json (~2 min CPU)
	$(PY) -m edge.train

web/node_modules: web/package-lock.json
	cd web && npm ci
	@touch web/node_modules

web/dist/index.html: web/node_modules $(WEB_SRC)
	cd web && npm run build

demo: web/dist/index.html    ## backend + web app + demo replay, opens the browser
	$(PY) -m server.demo --port $(PORT)

serve: web/dist/index.html   ## same, without starting the replay
	$(PY) -m server.demo --port $(PORT) --no-replay

dev: web/node_modules        ## backend on $(PORT) + Vite dev server with hot reload on 5173
	@trap 'kill 0' INT TERM; \
	$(PY) -m server.demo --port $(PORT) --no-replay --no-open & \
	cd web && npx vite --port 5173 --open

test:
	$(PY) -m pytest -q

lint:                        ## zero warnings everywhere
	$(PY) -m ruff check .
	$(PY) -m black --check -q edge server data tests
	cd web && npx eslint . --max-warnings 0 && npx prettier --check . --log-level warn && npx tsc -b

fmt:
	$(PY) -m ruff check . --fix -q
	$(PY) -m black -q edge server data tests
	cd web && npx prettier --write . --log-level warn

clean:
	rm -rf web/dist .pytest_cache .ruff_cache
