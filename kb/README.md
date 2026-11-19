# Knowledge base

What Granite retrieves before it plans (server/kb.py). Two kinds of chunks:

- **These guides** (`kb/*.md`, one `## ` section = one chunk): symptoms, what the sound does, safe hand
  checks, what the mechanic does, prevention. Written by people, general maintenance practice for small
  motors, pumps, fans and gearboxes.
- **The fault cards** in `server/faults.yaml`, turned into one chunk per fault by code: part, price range,
  labour, warranty, misuse, with the source of every figure.

Rules (tested in tests/test_kb.py):

- No ₹ amount and no part number is written in a guide. Prices and part numbers come only from
  `server/faults.yaml` (and from the tools), so there is one place to correct them.
- A guide never tells anyone to let software stop a machine. Stopping is the owner's decision; a hand check
  is always done with the machine switched off and locked out by the person doing it.
- Each guide starts with front matter: `title`, `faults` (faults.yaml keys) and `machines` (pump, fan,
  valve, slider, gearbox, or `any`).

Rebuild the vector index after editing: `.\dev.ps1 kb-build` (or `make kb-build`). Without Ollama the
search still works, by keywords.
