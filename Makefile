# Dev tasks for aiab. These drive the linters/type checker; they are not needed
# to *run* aiab (that's just `bin/aiab`). See the README "Development" section.
#
# Tooling is all apt-installable on Ubuntu:
#     sudo apt install python3-mypy black flake8 python3-pytest

PYTHON := python3
PY := aiab bin/aiab

.PHONY: check lint typecheck test format format-check

# Run everything CI would: formatting check, lint, type check, and tests.
check: format-check lint typecheck test

# Run the unit tests.
test:
	$(PYTHON) -m pytest tests/

# Lint with flake8 (config in .flake8).
lint:
	$(PYTHON) -m flake8 $(PY)

# Type-check with mypy (config in pyproject.toml; files = ["aiab"]).
typecheck:
	$(PYTHON) -m mypy

# Reformat in place with black (config in pyproject.toml).
format:
	$(PYTHON) -m black $(PY)

# Verify formatting without changing anything (what `check` runs).
format-check:
	$(PYTHON) -m black --check $(PY)
