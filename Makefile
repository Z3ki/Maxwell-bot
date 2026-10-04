PYTHON ?= python3

.PHONY: check dependencies lint shell-check test coverage

check: dependencies lint shell-check coverage

dependencies:
	$(PYTHON) -m pip check

lint:
	$(PYTHON) -m ruff check .

shell-check:
	$(PYTHON) scripts/check_shell_syntax.py

test:
	$(PYTHON) -m pytest -W error::RuntimeWarning

coverage:
	$(PYTHON) -m pytest -W error::RuntimeWarning --cov --cov-report=term-missing:skip-covered
