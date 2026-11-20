# Granite, the knowledge base, the Guardian and A2A

What changed in the agent layer, and how each part keeps the hard rules. Everything here runs on the
factory's own laptop: no cloud model, no API key, no per-token bill.

```
event (alarm, owner reply, mechanic, repair ...)
  │
  ├─ code first: apply the owner's numbered reply, re-score a measurement, note preferences
  ├─ context: case, memory, order book + knowledge (RAG, server/kb.py)
  │                                   + partner_offers (A2A, server/a2a, UNTRUSTED)
  ▼
Granite on Ollama (server/provider.py)  ── proposes tool calls ──►  ToolBox (server/planner/tools.py)
                                                                     ├─ schema + factory scope
                                                                     ├─ grounding: numbers, ₹, part numbers
                                                                     ├─ Guardian G1–G5 (server/planner/guardian.py)
                                                                     └─ approval gate + idempotent, audited actions
  ▼
final reply ── grounding + Guardian ──► sent, or the rules reply (refusal logged)
```

## A. The LLM provider: Granite on Ollama

- `server/provider.py` is a client in the shape the code already used (`messages.create`, `stream`, local
  batches), translated to Ollama's `/api/chat`: tools and tool calls, tool results, JSON-schema output, usage.
- Model: `DHADKAN_MODEL` (default `ibm/granite4.2:8b-q3_K_M`, tool calling); per-tier overrides
  `DHADKAN_MODEL_ROUTINE/PLAN/DIAGNOSE`. Embeddings: `DHADKAN_EMBED_MODEL` (default `granite-embedding`).
- The chat model is text-only: the second-look diagnosis gets a one-line note instead of the spectrogram
  image, plus the trend and the factory profile as text.
- Offline is normal: Ollama not running, the model not pulled, or `DHADKAN_LLM=0` means no client, and every
  path runs its rules. `.\dev.ps1 llm-check` shows provider, model, latency and one real tool call.
- Cost: ₹0 per token for a local model (`DHADKAN_LOCAL_INR_PER_MTOK` can price electricity and hardware); the
  per-case rupee cap still works on that figure.

## B. The knowledge base (RAG)

- `kb/*.md`: seven guides, one chunk per `## ` section, with front matter naming the faults and machine types.
  They hold **no ₹ amount and no part number**.
- `server/faults.yaml`: one generated card per fault: the price range or per-HP rate exactly as the file has
  it, its source, labour, warranty, misuse, "TYPICAL, NOT A QUOTE".
- Search: granite-embedding vectors (cached by text hash in `data/kb/index.npz`, never stale) blended with
  BM25; a named fault outranks keyword overlap; offline it is BM25 alone. The index is built in the
  background when the server starts (`.\dev.ps1 kb-build` by hand).
- The planner gets the chunks for the event's machine and fault up front (the fault card first), can call
  `search_knowledge`, and is asked to cite the ids it relied on. A ₹ figure is grounded only when a retrieved
  chunk carries it. Every retrieval is logged (`kb_retrieved`).
- Retrieval quality, `.\dev.ps1 kb-search` / `python -m server.kb eval` on `kb/eval_questions.json` (20
  owner-style questions, many in Hinglish, written by us): see `docs/runs/kb_eval_*.json`.

## C. The Guardian (anti-hallucination)

Grounding already refuses a number, ₹ amount or part number that no tool returned. The Guardian adds:

| rule | refuses |
|---|---|
| G1 no_control_claim | "I stopped the machine", "maine band kar diya", "मैंने बंद कर दिया" in a message from the agent (reply options in the owner's own voice are exempt) |
| G2 unknown_citation | a `[kb/...#...]` id that was not retrieved this turn |
| G3 unsupported_fault | a fault named to the owner that no tool and no retrieved chunk named |
| G4 untrusted_source | figures that only an external agent gave (they never ground anything) |
| G5 no_logged_approval | `book_job` / `order_part` without an approved, logged approval of that kind for that case (or, for a booking, the owner's own autonomy policy) |

Checked before the tool runs; the tool's own approval gate remains as a second, independent layer. Every
check is an audit row (`verdict`), passes included. A refused reply is replaced by the rules reply
(decision status `guardian_rejected`).

## D. A2A agent interop

- Protocol subset: JSON-RPC 2.0 `message/send`, Message or Task answers, text and data parts, agent cards at
  `/.well-known/agent-card.json` (and `agent.json`).
- **Dhadkan as an agent**: `POST /a2a`, card at `/.well-known/agent-card.json`. Skills:
  `maintenance_knowledge` (KB chunks with ids and sources) and `machine_status` (needs
  `Authorization: Bearer <DHADKAN_A2A_TOKEN>`; off without a token). An inbound message never creates an
  event, approval, booking or order; each call is logged (`a2a_inbound`).
- **Calling a friend's agent**: only partners in `data/demo/partners.json` (DEMO; add a row with a JSON-RPC
  URL to add one). No redirects, 10 s timeout, 64 kB limit, text clipped and stripped, every answer marked
  `untrusted`. On an alarm, code asks the mechanic partners for visit days (`partner_offers`); the planner
  can also call `ask_partner_agent`. Partners' words are kept out of grounding and the Guardian's facts, so
  their prices cannot reach the owner and an instruction inside them changes nothing (tested with a prompt
  injection: `.\dev.ps1 demo-agents --inject`).
- DEMO partners: `python -m server.a2a.demo_agents` serves "Ramesh Motor Works" (availability) and
  "Bhosari Bearings & Belts" (stock) on :8701. Invented, labelled DEMO in every answer.

## E. Evaluation

- `.\dev.ps1 eval-agent`: 40 planner scenarios on the scripted mock policy, including `kb_retrieved_and_cited`,
  `guardian_false_stop_claim`, `guardian_unknown_citation` and `a2a_partner_prompt_injection`. It measures
  the plumbing (gate, grounding, Guardian, re-planning), not Granite's judgement.
- `.\dev.ps1 eval-agent --live --only <name>`: the same on Granite. Slow on a CPU-only laptop (minutes per
  scenario); see the run records in `docs/runs/`.
- What is not measured yet: Granite's diagnosis accuracy on CWRU (`scripts/eval_understand.py`), and a full
  live run of every scenario.
