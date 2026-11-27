"""Environment and repository hygiene: the variable template, the README's secrets
paragraph, ``pmfa config check`` with no file, and test runs that leave no files behind."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.config import Config

from .support import README, ROOT

ENV_EXAMPLE = ROOT / ".env.example"
SOURCE = ROOT / "src" / "pmfa"
TESTS = ROOT / "tests"
USER_VARIABLE = re.compile(r"[\"']((?:PMFA|ANTHROPIC|KALSHI)_[A-Z0-9_]+)[\"']")
"""A string literal in the source that names a variable the user sets (the project's prefixes)."""
TEST_READ = re.compile(r"(?:environ\.get\(|environ\[|getenv\()[\"']([A-Z][A-Z0-9_]+)[\"']")
"""A variable a test reads from the environment (tests also name fake variables they set)."""

runner = CliRunner()


def variables_read() -> set[str]:
    """Every user-set environment variable named in the source, the configuration, or the tests."""
    names = {
        name
        for path in SOURCE.rglob("*.py")
        for name in USER_VARIABLE.findall(path.read_text(encoding="utf-8"))
    }
    names |= {
        name
        for path in TESTS.rglob("*.py")
        for name in TEST_READ.findall(path.read_text(encoding="utf-8"))
        if name.startswith("PMFA_") and not name.startswith("PMFA_PROBE")  # probe-only
    }
    config = Config()
    for section in (config.models, config.kalshi):
        names.update(
            value
            for field, value in section.model_dump().items()
            if field.endswith("_env") and isinstance(value, str)
        )
    return names


def test_env_example_lists_every_variable_the_code_reads() -> None:
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))
    names = variables_read()
    assert {"ANTHROPIC_API_KEY", "PMFA_CONFIG", "PMFA_MINILM_CACHE", "PMFA_SMOKE"} <= names
    assert names - listed == set()


def test_env_example_and_readme_say_variables_are_exported_not_loaded_from_dotenv() -> None:
    template = ENV_EXAMPLE.read_text(encoding="utf-8")
    header = " ".join(line.lstrip("# ") for line in template.splitlines()[:6])
    assert "export" in header and "never reads a .env file" in header
    assert "Copy to .env" not in template
    readme = " ".join(README.read_text(encoding="utf-8").split())
    assert "export ANTHROPIC_API_KEY=..." in readme
    assert "Nothing reads a `.env` file" in readme
    assert "makes configuration loading fail" not in readme  # an empty PMFA_CONFIG is "not set"


def test_config_check_without_a_file_says_it_checked_the_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PMFA_CONFIG", raising=False)
    out = runner.invoke(app, ["config", "check"])
    assert out.exit_code == 0, out.output
    assert "pmfa.toml: ok" not in out.output
    assert "no pmfa.toml" in out.output and "built-in defaults: ok" in out.output

    monkeypatch.setenv("PMFA_CONFIG", "")  # empty means not set
    out = runner.invoke(app, ["config", "check"])
    assert out.exit_code == 0 and "built-in defaults: ok" in out.output

    (tmp_path / "pmfa.toml").write_text("[budget]\nmax_calls_per_episode = 12\n")
    out = runner.invoke(app, ["config", "check"])
    assert out.exit_code == 0 and out.output.strip() == "pmfa.toml: ok"


def test_hypothesis_keeps_no_database_and_writes_nothing_into_the_repository() -> None:
    from hypothesis import settings
    from hypothesis.configuration import storage_directory

    assert settings.default.database is None
    assert settings.default.derandomize
    storage = storage_directory("constants", intent_to_write=False).path.resolve()
    assert not storage.is_relative_to(ROOT.resolve())
