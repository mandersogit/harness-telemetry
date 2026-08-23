.PHONY: help install test test-cov lint lint-fix format format-check \
        typecheck typecheck-mypy typecheck-pyright \
        lint-md lint-md-tables format-md format-md-check all clean

# An optional, untracked `Makefile.local` may override MD_PATHS (e.g.
# MD_PATHS := docs notes) to widen markdown discovery. It is optional by design:
# nothing here requires it, and this file names no directory that may not exist.
-include Makefile.local

# --- interpreter / venv ------------------------------------------------------
# `install` creates $(VENV)/ the first time only; every other target uses
# $(VENV)/bin/* directly. Bootstrapping needs a CPython >=3.11 interpreter —
# supply it as PYTHON, either an env var or on the command line:
#   PYTHON=python3.11 make install
#   make install PYTHON=/usr/local/bin/python3.11
# No fallback chain: if $(VENV)/bin/python doesn't exist and PYTHON is unset,
# `install` fails with a two-line message (see the recipe below).
VENV       := local.venv

PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
PYRIGHT := $(VENV)/bin/pyright

# Markdown toolchain: mdformat (Python, in the dev venv) for formatting;
# markdownlint-cli2 (Node) for linting, resolved from PATH. Override with
# MARKDOWNLINT_CLI2=/path/to/markdownlint-cli2 if it isn't on PATH.
MARKDOWNLINT_CLI2 ?= markdownlint-cli2
TABLE_MAX_COLS    := 150

# Markdown discovery: docs/ and any *.md at the repo root by default. A local
# Makefile.local (-include'd above) may widen MD_PATHS; `find ... 2>/dev/null`
# below tolerates a listed directory that does not exist.
MD_PATHS ?= docs
MD_FILES := $$(find $(MD_PATHS) -name '*.md' 2>/dev/null) $(wildcard *.md)

help:
	@echo "harness-telemetry targets:"
	@echo ""
	@echo "  install         — create $(VENV)/ if missing (PYTHON=... make install the"
	@echo "                    first time) and pip install -e '.[dev]'"
	@echo ""
	@echo "  test            — pytest (synthetic tests; the maintainer's real-capture"
	@echo "                    suite lives under tests/local/ and is not distributed)"
	@echo "  test-cov        — pytest with coverage report"
	@echo ""
	@echo "  lint            — ruff check src/ tests/"
	@echo "  lint-fix        — ruff check --fix"
	@echo "  format          — ruff format src/ tests/"
	@echo "  format-check    — ruff format --check"
	@echo "  typecheck       — mypy (strict) + pyright (basic)"
	@echo "  all             — lint + format-check + typecheck + test"
	@echo ""
	@echo "  lint-md         — markdownlint-cli2 + the $(TABLE_MAX_COLS)-col table check"
	@echo "  lint-md-tables  — table-line-length check only"
	@echo "  format-md       — mdformat --wrap keep (no paragraph re-wrapping)"
	@echo "  format-md-check — mdformat --check (CI-style)"
	@echo ""
	@echo "  clean           — remove build/cache artifacts"

# --- install -----------------------------------------------------------------

install:
	@test -x $(PY) || { \
	    test -n "$(PYTHON)" || { \
	        echo "error: $(VENV)/ does not exist and PYTHON is not set." >&2; \
	        echo "       bootstrap with: PYTHON=python3.11 make install   (any CPython >= 3.11)" >&2; \
	        exit 2; }; \
	    "$(PYTHON)" -m venv $(VENV); }
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

# --- tests -------------------------------------------------------------------

test:
	$(PYTEST)

test-cov:
	$(PYTEST) --cov --cov-report=term-missing

# --- code quality ------------------------------------------------------------

lint:
	$(RUFF) check src/ tests/

lint-fix:
	$(RUFF) check --fix src/ tests/

format:
	$(RUFF) format src/ tests/

format-check:
	$(RUFF) format --check src/ tests/

typecheck: typecheck-mypy typecheck-pyright

typecheck-mypy:
	$(MYPY)

typecheck-pyright:
	$(PYRIGHT) src/

all: lint format-check typecheck test

# --- markdown ----------------------------------------------------------------
# Not part of `all` so the default dev loop stays fast. Per-file workflow for
# live edits:
#   $(PY) -m mdformat --wrap keep <FILE>
#   $(MARKDOWNLINT_CLI2) <FILE>

# 150-column-on-tables check: markdownlint's MD013 has only one line_length
# setting, so it can't enforce a length on tables while leaving prose alone.
# Flags any line whose first non-whitespace character is `|`. Python (not awk)
# because mawk's length() counts BYTES — `·`/`✓`/`²` in the coverage tables
# would inflate the count; the rule is about display columns (characters).
lint-md-tables:
	@$(PY) -c 'import sys; \
	max_cols = int(sys.argv[1]); \
	bad = [(f, n, len(line)) \
	    for f in sys.argv[2:] \
	    for n, line in enumerate((x.rstrip("\n") for x in open(f, encoding="utf-8")), 1) \
	    if line.lstrip().startswith("|") and len(line) > max_cols]; \
	[print(f"{f}:{n}:{max_cols+1} error table-line-length (actual: {c}, max: {max_cols})") for f, n, c in bad]; \
	sys.exit(1 if bad else 0)' $(TABLE_MAX_COLS) $(MD_FILES)

lint-md:
	@command -v $(MARKDOWNLINT_CLI2) >/dev/null 2>&1 || { \
	    echo "$(MARKDOWNLINT_CLI2) not found on PATH" >&2; \
	    echo "install with: npm install -g markdownlint-cli2" >&2; \
	    exit 127; }
	$(MARKDOWNLINT_CLI2) $(MD_FILES)
	@$(MAKE) --no-print-directory lint-md-tables

format-md:
	$(PY) -m mdformat --wrap keep $(MD_FILES)

format-md-check:
	$(PY) -m mdformat --wrap keep --check $(MD_FILES)

# --- cleanup -----------------------------------------------------------------

clean:
	rm -rf build/ dist/ src/*.egg-info
	find . -path ./local.venv -prune -o -type d -name __pycache__ -print -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage
