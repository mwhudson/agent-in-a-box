# Development

The code is type-hinted and kept clean with [black](https://black.readthedocs.io/)
(formatting), [flake8](https://flake8.pycqa.org/) (linting), and
[mypy](https://mypy-lang.org/) (type checking). Tests use
[pytest](https://docs.pytest.org/). All four are apt packages, so no virtualenv
is needed:

```sh
sudo apt install python3-mypy black flake8 python3-pytest
```

A `Makefile` wraps them (configuration lives in `pyproject.toml` and `.flake8`):

```sh
make check         # what CI runs: format-check + lint + typecheck + test
make test          # run the test suite with pytest
make format        # reformat in place with black
make lint          # flake8
make typecheck     # mypy
```
