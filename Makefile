.PHONY: all lint format test test-unit test-fast test-integration test-matrix ruff-report bandit-report sonar-inputs tach security docstrings complexity deadcode reuse typecheck check install install-dev docs docs-build clean spdx

REPORTS_DIR ?= reports
COVERAGE_XML ?= $(REPORTS_DIR)/coverage.xml
COVERAGE_JSON ?= $(REPORTS_DIR)/coverage.json
UNIT_JUNIT_XML ?= $(REPORTS_DIR)/unit.junit.xml
INTEGRATION_JUNIT_XML ?= $(REPORTS_DIR)/integration.junit.xml
RUFF_REPORT ?= $(REPORTS_DIR)/ruff-report.json
BANDIT_REPORT ?= $(REPORTS_DIR)/bandit-report.json

all: check

test: test-unit

# Run linter and format checker (fast, run before commits)
lint:
	@if LC_ALL=C grep -nP '[^\x00-\x7F]' pyproject.toml; then echo "pyproject.toml must be ASCII-only"; exit 1; fi
	mkdir -p $(REPORTS_DIR)
	uv run ruff check --exit-zero --output-format=json --output-file=$(RUFF_REPORT) .
	uv run ruff check .
	uv run ruff format --check .

# Auto-fix lint issues and format code
format:
	uv run ruff check --fix .
	uv run ruff format .

# Fast dev loop: run only the tests affected by the branch diff (tach
# impact analysis), no coverage.  Impact analysis follows the Python
# import graph only — after touching non-Python inputs (YAML, templates,
# scripts) run the full `make test` instead.
test-fast:
	uv run pytest tests/unit/ --tach

# Run tests with coverage
test-unit:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/unit/ --cov=terok_util --cov-report=term-missing --cov-report=xml:$(COVERAGE_XML) --cov-report=json:$(COVERAGE_JSON) --junitxml=$(UNIT_JUNIT_XML) -o junit_family=legacy

# Deep-reach integration tests — kernel surfaces, real uids, a fresh
# interpreter.  No podman, no network: they run anywhere pytest does,
# and skip the branch (root / non-root) this host cannot be.
test-integration:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/integration/ -v --junitxml=$(INTEGRATION_JUNIT_XML) -o junit_family=legacy

# Multi-distro test matrix — slots declared in tests/containers/matrix.yml,
# engine provided by this very package (terok-matrix).
# Options (env vars):
#   NO_CACHE=1    Rebuild images from scratch (ignore layer cache)
#   BUILD_ONLY=1  Build images without running tests
#   SCOPE=unit    Run only unit tests (or: integ)
#   SLOTS="fedora43 debian13"  Run specific slots only
#   JOBS=4        Run up to N slots concurrently (live output, [slot]-tagged lines)
# `make -j 4 test-matrix` works too: GNU make >= 4.3 exposes -jN in MAKEFLAGS,
# and JOBS defaults to it.  An explicit JOBS= always wins; bare -j (unlimited)
# carries no number and falls back to serial.
MAKE_JOBS = $(patsubst -j%,%,$(filter -j%,$(MAKEFLAGS)))
JOBS ?= $(MAKE_JOBS)
test-matrix:
	uv run terok-matrix \
		$(if $(NO_CACHE),--no-cache) \
		$(if $(BUILD_ONLY),--build-only) \
		$(if $(filter unit,$(SCOPE)),--unit-only) \
		$(if $(filter integ,$(SCOPE)),--integ-only) \
		$(if $(JOBS),--jobs $(JOBS)) \
		$(SLOTS)

# Write Ruff's JSON report without failing on findings.
ruff-report:
	mkdir -p $(REPORTS_DIR)
	uv run ruff check --exit-zero --output-format=json --output-file=$(RUFF_REPORT) .

# Write Bandit's JSON report without failing on findings.  Uses the
# same [tool.bandit] config the ``security`` target consumes so the
# Sonar import sees the same skip set we audited locally.
bandit-report:
	mkdir -p $(REPORTS_DIR)
	uv run bandit -c pyproject.toml -r src/terok_util/ --exit-zero -f json -o $(BANDIT_REPORT)

# Generate the files SonarQube Cloud imports from reports/.
sonar-inputs: test-unit ruff-report bandit-report

# Check module boundary rules (tach.toml)
tach:
	uv run tach check

# Run SAST security scan.  Runs at default severity (low+) to match
# SonarCloud's strictness — see [tool.bandit] in pyproject.toml for
# the project-wide skips.
security:
	mkdir -p $(REPORTS_DIR)
	uv run bandit -c pyproject.toml -r src/terok_util/ --exit-zero -f json -o $(BANDIT_REPORT)
	uv run bandit -c pyproject.toml -r src/terok_util/

# Check docstring coverage (minimum 95%)
docstrings:
	uv run docstr-coverage src/terok_util/ --fail-under=95

# Check cognitive complexity (advisory — lists functions exceeding threshold)
complexity:
	uv run complexipy src/terok_util/ --max-complexity-allowed 15 --failed; true

# Find dead code (cross-file, min 80% confidence)
deadcode:
	uv run vulture src/terok_util/ vulture_whitelist.py --min-confidence 80

# Static type check with mypy.
typecheck:
	uv run mypy src/terok_util/ $(MYPYFLAGS)

# Check REUSE (SPDX license/copyright) compliance
reuse:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	uv run reuse lint

# Add SPDX header to files.
# NAME must be the real name of the person responsible for creating the file (not a project name).
# Example: make spdx NAME="Real Human Name" FILES="src/terok_util/foo.py"
spdx:
ifndef NAME
	$(error NAME is required — use the real name of the copyright holder, e.g. make spdx NAME="Real Human Name" FILES="src/terok_util/foo.py")
endif
	uv run reuse annotate --template compact --copyright "$(NAME)" --license Apache-2.0 $(FILES)

# Run all checks (equivalent to CI)
check: lint test-unit tach typecheck security docstrings deadcode reuse

# Install runtime dependencies only
install:
	uv sync --no-default-groups

# Install all dependencies (dev, test, docs)
install-dev:
	uv sync --all-groups
	uv run pre-commit install

# Build documentation locally
docs:
	uv run properdocs serve

# Build documentation for deployment
docs-build:
	uv run properdocs build --strict

# Clean build artifacts
clean:
	rm -rf dist/ build/ site/ reports/ .coverage .pytest_cache/ .ruff_cache/ .complexipy_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
