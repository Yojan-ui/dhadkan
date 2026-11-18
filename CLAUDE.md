# Dhadkan: working notes for Claude

- **Windows PowerShell only.** This checkout is developed on native Windows: no bash, no WSL, no make.
  Never call `make`. Use `.\dev.ps1 <task>` (`setup`, `test`, `lint`, `fmt`, `demo`, `demo-ims`, `serve`,
  `dev`, `mill`, `compare`, `rul`, ...; `.\dev.ps1 help` lists them). The Makefile stays as the
  Linux/macOS path; keep the two in step when a target changes.
- Python is `.venv\Scripts\python` (never `.venv/bin/python`). The repo path contains a space, so quote paths.
- Use `pathlib` for paths. No POSIX-only calls: no `fcntl`, `signal.SIGKILL`, `os.symlink`, `os.fork`,
  hard-coded `/tmp`, or shell pipelines in Python.
- Always open text files with `encoding="utf-8"` (`read_text`, `write_text`, `open`): the repo holds
  Hindi, Marathi, Gujarati, Tamil, Kannada and Telugu text, and the Windows default codepage corrupts it.
- Secrets live in `.env` (git-ignored; template in `.env.example`) and are never committed.
- Before a commit: `.\dev.ps1 lint` (zero warnings) and `.\dev.ps1 test` both pass.
- Guardrails are tested in `tests/test_guardrails.py`: no code path can stop or control a machine; paid
  actions need a logged owner approval for the same case; ₹ amounts and part numbers come only from
  `server/faults.yaml` and tool outputs; demo / mock / simulated data stays labelled as such.
