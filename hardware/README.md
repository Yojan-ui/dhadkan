# Hardware

| path | what | status |
|---|---|---|
| phone microphone | 10 s readings, scored on the phone (`edge/runtime.py`, `.tflite`) | no Android app yet; laptop timings only |
| `esp32_vibration/` | ESP32 + MPU6050: 2 s of Z-axis vibration at ~1 kHz every 10 min, offline queue, sequence numbers | **not tested on hardware**; server side tested with the simulated device |
| `tools/sim_device.py` | a simulated device that posts recordings or synthetic vibration to `/api/ingest/reading`, labelled `simulated` | tested (`tests/test_ingest.py`) |

Every reading is stored with its source: `live` (a device in a shed), `upload` (a recording someone
sent) or `simulated`. The dashboard shows the label. Inference latency and memory on a phone or an
ESP32 have **not** been measured; docs/RESULTS.md has the laptop numbers and says so.
