# Copilot Port

Single-file converter (`copilot_port.py`) from Agent Skills / Claude / Codex agents to GitHub Copilot customizations. Python 3.11+, PyYAML only.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Test

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Smoke test

```sh
.venv/bin/python copilot_port.py ~/.codex/skills --target "$(mktemp -d)"
```
