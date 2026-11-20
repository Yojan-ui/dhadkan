# Dhadkan. From a fresh clone: make setup && make demo-data && make demo
PY      ?= .venv/bin/python
# Python 3.12 builds the venv (TensorFlow wheels); override with make setup PYTHON=/path/to/python3.12
PYTHON  ?= $(shell command -v python3.12 2>/dev/null || command -v python3 2>/dev/null)
PORT    ?= 8600
WEB_SRC := $(shell find web/src web/index.html web/public -type f 2>/dev/null) web/package.json web/vite.config.ts

.PHONY: setup data demo-data train results demo demo-ims serve dev test lint fmt clean compare rul mill mill-pack eval-agent llm-check kb-build kb-search demo-agents tunnel live-trial voice-models tts-eval

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

demo-ims: web/dist/index.html  ## Digital Twin + NASA IMS bearing failure + agent (what npm run demo runs)
	$(PY) -m server.demo --port $(PORT) --ims

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

eval-agent:                  ## the planner's scenarios on the mock policy; LIVE=1 runs them on local Granite (Ollama)
	$(PY) -m server.planner.evaluate $(if $(LIVE),--live,)
	$(PY) -m edge.results

llm-check:                   ## is Granite answering through Ollama? provider, model, latency, one real tool call
	$(PY) -m server.llm_check

voice-models:                ## optional voice notes: CPU torch, sherpa-onnx, transformers and the speech models
	$(PY) -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu
	$(PY) -m pip install -q -r requirements-voice.txt
	$(PY) -m scripts.fetch_voice_models

tts-eval:                    ## compare the TTS engines on this machine -> docs/runs/tts_<UTC>.json
	$(PY) -m scripts.tts_eval

tunnel:                      ## HTTPS for the phone's mic: cloudflared (or ngrok) to the backend, URL + QR code
	$(PY) -m scripts.tunnel --port $(PORT)

live-trial:                  ## measure the stage trial: make live-trial M=A-F1 (docs/runs/live_<UTC>.json)
	$(PY) -m scripts.live_trial $(M)

demo-agents:                 ## two DEMO partner agents over A2A on :8701 (INJECT=1: the dealer tries a prompt injection)
	$(PY) -m server.a2a.demo_agents $(if $(INJECT),--inject,)

kb-build:                    ## embed the knowledge base (kb/*.md, faults.yaml) with granite-embedding
	$(PY) -m server.kb build

kb-search:                   ## search the knowledge base: make kb-search Q="pump gravel noise"
	$(PY) -m server.kb search "$(Q)"

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
