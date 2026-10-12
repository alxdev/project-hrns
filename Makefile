PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)
RUFF ?= $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
MYPY ?= $(if $(wildcard .venv/bin/mypy),.venv/bin/mypy,mypy)

.PHONY: test lint format-check format type-check check

test:
	$(PYTHON) -m pytest

lint:
	$(RUFF) check .

format-check:
	$(RUFF) format --check .

format:
	$(RUFF) check . --fix
	$(RUFF) format .

type-check:
	$(MYPY) src

check: lint format-check type-check test
