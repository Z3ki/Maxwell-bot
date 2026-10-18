PYTHON ?= python3

.PHONY: check dependencies lint format-check typecheck audit shell-check test coverage

CORE_COVERAGE_FAIL_UNDER ?= 83

check: dependencies lint format-check typecheck shell-check coverage audit

dependencies:
	$(PYTHON) -m pip check

lint:
	$(PYTHON) -m ruff check .

# Only new test modules are held to the formatter; see tests.yml.
format-check:
	$(PYTHON) -m ruff format --check tests/unit

typecheck:
	$(PYTHON) -m mypy

audit:
	$(PYTHON) -m pip_audit -r requirements.txt -r requirements-dev.txt
	$(PYTHON) -m bandit -q -r . -ll -ii -x ./tests,./.venv,./venv,./data,./temp,./shelldocker -b .bandit-baseline.json

shell-check:
	$(PYTHON) scripts/check_shell_syntax.py

test:
	$(PYTHON) -m pytest -W error::RuntimeWarning

coverage:
	$(PYTHON) -m pytest -W error::RuntimeWarning --cov --cov-report=term-missing:skip-covered
	$(PYTHON) -m coverage report --skip-covered --include='maxwell_core/*,message_pipeline.py,concurrency_safety.py' --fail-under=$(CORE_COVERAGE_FAIL_UNDER)

# Runtime lifecycle is guarded and scoped by scripts/dev.sh, never production.
.PHONY: updev restartdev testdev statusdev logsdev stopdev youtubeauthdev
updev:
	bash scripts/dev.sh up

restartdev:
	bash scripts/dev.sh restart

testdev:
	bash scripts/dev.sh test $(PYTEST_ARGS)

statusdev:
	bash scripts/dev.sh status

logsdev:
	bash scripts/dev.sh logs

stopdev:
	bash scripts/dev.sh stop

youtubeauthdev:
	bash scripts/dev.sh youtube-auth
