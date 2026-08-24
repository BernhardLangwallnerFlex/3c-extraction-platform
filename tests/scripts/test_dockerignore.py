"""`.dockerignore` rules actually take effect in `az acr build`.

`az acr build` does not use Docker's .dockerignore parser. It builds the context
tarball itself and its IgnoreRule class strips a trailing slash only inside the
`if rule.startswith('!')` branch, so a plain directory rule keeps its slash:

    temp/  ->  regex ^temp/$  vs tar entry "temp"  ->  never matches

Every directory rule in .dockerignore carried a trailing slash until 2026-08-24,
so none had ever taken effect. The context tarball was 2.8 GB and took ~17 min
to upload per product, against a 1m41s build. Only .venv escaped, via the CLI's
own hardcoded build_ignore_dirs.

The trailing-slash test below needs no Azure CLI and is the real regression
guard. The rest drive the actual IgnoreRule class when the CLI is installed, so
we assert against its behaviour rather than against assumed Docker semantics.
"""
import glob
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO / ".dockerignore"

# Uploaded on every build if the rules do not bite; sizes as of 2026-08-24.
HEAVY = ["temp", "test_uploads", "3C_testdaten_pdf", "GOT", "garagenhub_input",
         "bps_sanierer_input", "handover_pruefung", "old", "benchmarks"]

# The Dockerfile COPYs exactly these. None may be masked.
REQUIRED = ["requirements.prod.txt", "core", "core/api/main.py",
            "core/jobs/worker.py", "products", "products/__init__.py",
            "products/sanierer/extract_schema.json"]


def _rules():
    out = []
    for line in DOCKERIGNORE.read_text().splitlines():
        rule = line.rstrip()
        if not rule or rule.startswith("#"):
            continue
        out.append(rule)
    return out


def test_no_rule_has_a_trailing_slash():
    """The whole bug in one assertion — a trailing slash silently disables a rule."""
    offenders = [r for r in _rules() if r.rstrip("!").endswith("/") or r.endswith("/")]

    assert offenders == [], (
        f"these rules end in '/' and will be silently ignored by az acr build: {offenders}"
    )


def test_the_heavy_directories_are_all_listed():
    """A new corpus directory is easy to add and easy to forget to exclude."""
    rules = set(_rules())
    missing = [d for d in HEAVY if d not in rules]

    assert missing == [], f"not excluded from the ACR build context: {missing}"


def test_required_paths_are_not_excluded():
    """*.csv / *.pdf style rules apply inside core/ too, so they can mask a COPY."""
    rules = _rules()
    for req in REQUIRED:
        assert req not in rules, f"{req} is COPYd by the Dockerfile but is excluded"


def test_json_is_not_blanket_excluded():
    """products/*/extract_schema.json is required at runtime."""
    assert "*.json" not in _rules(), "*.json would strip the product schemas from the image"


def _ignore_rule_class():
    """The real class from the installed Azure CLI, or skip."""
    try:
        from azure.cli.command_modules.acr._archive_utils import IgnoreRule
        return IgnoreRule
    except ImportError:
        pass
    for pattern in ("/opt/homebrew/Cellar/azure-cli/*/libexec/lib/python*/site-packages",
                    "/usr/lib/azure-cli/lib/python*/site-packages",
                    "/usr/local/Cellar/azure-cli/*/libexec/lib/python*/site-packages"):
        for path in glob.glob(pattern):
            if path not in sys.path:
                sys.path.insert(0, path)
            try:
                from azure.cli.command_modules.acr._archive_utils import IgnoreRule
                return IgnoreRule
            except ImportError:
                continue
    pytest.skip("Azure CLI not importable; trailing-slash test covers the regression")


def _compile(IgnoreRule, rules):
    """Mirror _load_dockerignore_file: later rules win, so it checks in reverse."""
    return [IgnoreRule(r) for r in reversed(rules)]


def _is_ignored(compiled, name):
    for rule in compiled:
        if re.match(rule.pattern, name):
            return rule.ignore
    return False


@pytest.mark.parametrize("path", HEAVY)
def test_real_cli_parser_excludes_each_heavy_directory(path):
    IgnoreRule = _ignore_rule_class()

    assert _is_ignored(_compile(IgnoreRule, _rules()), path) is True


@pytest.mark.parametrize("path", REQUIRED)
def test_real_cli_parser_keeps_every_required_path(path):
    IgnoreRule = _ignore_rule_class()

    assert _is_ignored(_compile(IgnoreRule, _rules()), path) is False
