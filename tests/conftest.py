"""Suite-wide settings: Hypothesis runs deterministically and keeps its files in a temporary directory.

Hypothesis would otherwise keep an example database and a constants cache in
``.hypothesis/`` in the working directory, the repository root. The profile
below turns the database off and derandomizes generation, so every run tries
the same examples. Its storage directory, where the constants cache lives, is
moved to a temporary directory before collection (collecting the property
tests already writes to it) and removed when the run ends.
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

try:
    from hypothesis import settings
except ImportError:  # pragma: no cover - hypothesis is in the dev group
    settings = None

if settings is not None:
    settings.register_profile("pmfa", database=None, derandomize=True)
    settings.load_profile("pmfa")


def pytest_configure(config: pytest.Config) -> None:
    """Point Hypothesis's storage directory at a temporary directory for this run."""
    if settings is None:
        return
    from hypothesis.configuration import set_hypothesis_home_dir

    home = tempfile.mkdtemp(prefix="pmfa-hypothesis-")
    set_hypothesis_home_dir(home)
    config.add_cleanup(lambda: shutil.rmtree(home, ignore_errors=True))
