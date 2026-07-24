VENV := .venv
PYTHON := $(VENV)/bin/python
UV := $(VENV)/bin/uv

.PHONY: venv install install-dev lint format format-check typecheck test audit build clean activate

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install uv

install: venv
	$(UV) pip install -e .

install-dev: install
	$(UV) pip install ruff ty pytest pytest-asyncio esphome pip-audit bandit

lint: install-dev
	$(VENV)/bin/ruff check src/ tests/

format: install-dev
	$(VENV)/bin/ruff format src/ tests/

format-check: install-dev
	$(VENV)/bin/ruff format --check src/ tests/

typecheck: install-dev
	$(VENV)/bin/ty check src/

test: install-dev
	$(VENV)/bin/pytest tests/ -v

audit: install
	$(VENV)/bin/pip freeze --exclude-editable > audit-requirements.txt
	$(UV) pip install pip-audit bandit
	$(VENV)/bin/pip-audit -r audit-requirements.txt
	$(VENV)/bin/bandit -ll -ii -s B104 -r src/

build: install
	$(UV) pip install build
	$(PYTHON) -m build

check: lint format-check typecheck test

clean:
	rm -rf $(VENV) dist/ build/ *.egg-info src/*.egg-info

activate: install-dev
	@echo "Entering venv shell. Type 'exit' to leave."
	@VIRTUAL_ENV=$(CURDIR)/$(VENV) PATH=$(CURDIR)/$(VENV)/bin:$(PATH) $(SHELL)
