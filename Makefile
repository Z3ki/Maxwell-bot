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
