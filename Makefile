# Dhadkan. From a fresh clone: make setup && make demo-data && make demo
PY      ?= .venv/bin/python
# Python 3.12 builds the venv (TensorFlow wheels); override with make setup PYTHON=/path/to/python3.12
PYTHON  ?= $(shell command -v python3.12 2>/dev/null || command -v python3 2>/dev/null)
PORT    ?= 8600
WEB_SRC := $(shell find web/src web/index.html web/public -type f 2>/dev/null) web/package.json web/vite.config.ts

.PHONY: setup data demo-data train results demo serve dev test lint fmt clean compare rul mill mill-pack

setup:                       ## venv, Python and web dependencies
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else "Python 3.12+ needed: make setup PYTHON=/path/to/python3.12")'
	@$(PYTHON) -c 'import sys; v = sys.version_info[:2]; v != (3, 12) and print(f"note: Python {v[0]}.{v[1]}; tested on 3.12")'
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -q -r requirements.txt -r requirements-dev.txt
	cd web && npm ci

data:                        ## DCASE 2020 Task 2 (test split + training subset), NASA IMS, CWRU, MIMII DUE gearbox
	$(PY) -m data.real
	$(PY) -m data.download

demo-data:                   ## only the clips the dashboard replays (198 MB, ~6 min on a 0.6 MB/s link); Demo mode needs none
	$(PY) -m data.real demo
	$(PY) -m data.download

train:                       ## anomaly models, fault-type classifier, IMS run-to-failure, docs/RESULTS.md
	$(PY) -m edge.train
	$(PY) -m edge.faulttype
	$(PY) -m edge.ims
	$(PY) -m edge.results

results:                     ## regenerate docs/RESULTS.md from the newest evaluation records
	$(PY) -m edge.results

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

compare:                     ## choose the per-machine detector on selection units, report on held-out units
	$(PY) -m edge.compare

rul:                         ## time to maintenance threshold, leave one NASA IMS bearing out
	$(PY) -m edge.rul

mill:                        ## the flour-mill story headless (owner auto-approves): docs/runs/flourmill_<UTC>.json
	$(PY) -m server.flourmill --auto

mill-pack:                   ## rebuild edge/models/flourmill_pack.npz from MIMII DUE gearbox recordings
	$(PY) -m server.flourmill --pack

test:
	$(PY) -m pytest -q
	cd web && npm test --silent

lint:                        ## zero warnings everywhere
	$(PY) -m ruff check .
	$(PY) -m black --check -q edge server data tests tools scripts
	cd web && npx eslint . --max-warnings 0 && npx prettier --check . --log-level warn && npx tsc -b

fmt:
	$(PY) -m ruff check . --fix -q
	$(PY) -m black -q edge server data tests tools scripts
	cd web && npx prettier --write . --log-level warn

clean:
	rm -rf web/dist .pytest_cache .ruff_cache
