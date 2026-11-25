"""What the runner does before any pass: refusing cleanly, choosing models, recording the code."""

from __future__ import annotations

from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.errors import ConfigError
from pmfa.evaluation.backtest import run_backtest
from tests.evaluation.backtest.support import make_config


def test_a_refused_run_leaves_no_run_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path / "data")  # no snapshot was built
    run_dir = tmp_path / "runs" / "refused"
    with pytest.raises(ConfigError, match="no snapshot"):
        run_backtest(config, run_dir=run_dir, systems=["market"], dry_run=True)
    assert not run_dir.exists()


def test_the_live_gateway_is_built_with_tracing_switched_off(
    data_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import pmfa.models.gateway as gateway_module
    from pmfa.evaluation import backtest
    from pmfa.world.live import TRACING_VARIABLES

    for name in TRACING_VARIABLES:
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv(data_config.models.api_key_env, "sk-test-not-a-key")
    seen: dict[str, str] = {}

    class FakeGateway:
        def __init__(self, config: Config, **kwargs: object) -> None:
            seen.update({name: os.environ[name] for name in TRACING_VARIABLES})

    monkeypatch.setattr(gateway_module, "Gateway", FakeGateway)
    models, source = backtest._choose_models(None, data_config, dry_run=False)
    assert source == "gateway" and isinstance(models, FakeGateway)
    assert seen and all(value == "false" for value in seen.values())


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_git_details_name_the_changed_and_untracked_paths_from_the_root(tmp_path: Path) -> None:
    from pmfa.evaluation.manifest import git_details

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("a = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    clean = git_details(repo / "src")
    assert clean.commit and clean.dirty is False and clean.changes == []

    (repo / "src" / "a.py").write_text("a = 2\n")
    (repo / "src" / "new.py").write_text("b = 1\n")
    dirty = git_details(repo / "src")  # asked from a subdirectory, answered for the whole tree
    assert dirty.dirty is True
    assert dirty.changes == ["src/a.py", "src/new.py"]
    assert git_details(tmp_path).commit is None  # not a repository


def test_git_details_default_to_the_code_not_the_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pmfa
    from pmfa.evaluation.manifest import git_details

    if git_details(Path(pmfa.__file__).parent).commit is None:
        pytest.skip("pmfa is not running from a git checkout")
    monkeypatch.chdir(tmp_path)  # not a repository
    state = git_details()
    assert state.commit is not None and state.root is not None
    assert Path(pmfa.__file__).resolve().is_relative_to(Path(state.root).resolve())


def test_the_manifest_identifies_the_data_and_a_dirty_tree(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.evaluation import backtest
    from pmfa.evaluation.manifest import GitState, file_sha256, hashes_digest, read_manifest
    from pmfa.evaluation.report import build_report, publish_run
    from pmfa.world.evidence.corpus import CorpusStore
    from pmfa.world.snapshot.store import snapshot_path

    monkeypatch.setattr(
        backtest,
        "git_details",
        lambda: GitState(commit="abc1234", dirty=True, changes=["src/pmfa/x.py"], root="/r"),
    )
    run_dir = tmp_path / "run"
    manifest = run_backtest(data_config, run_dir=run_dir, systems=["market"])
    assert manifest.git_changes == ["src/pmfa/x.py"]
    assert manifest.snapshot_sha256 == file_sha256(snapshot_path(data_config))
    corpus = CorpusStore(data_config.paths.corpus_dir()).manifest()
    assert corpus is not None and corpus.content_hashes
    assert manifest.corpus_hashes_digest == hashes_digest(corpus.content_hashes)

    text = build_report(run_dir).read_text()
    assert "uncommitted changes (1 path(s) changed or untracked: src/pmfa/x.py)" in text
    assert "does not hold exactly the code that produced it" in text
    target = publish_run(run_dir, "dirty", root=tmp_path / "published")
    published = read_manifest(target)
    assert any("does not hold exactly the code" in n for n in published.notes)
    files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert {o for o in published.outputs if "/" not in o} == {f for f in files if "/" not in f}
    assert {o.split("/")[0] for o in published.outputs} == {f.split("/")[0] for f in files}
