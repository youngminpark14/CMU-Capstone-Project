"""Every configuration field is read by the code, so a knob in pmfa.toml always does something.

A field that is declared, validated and written to ``pmfa.toml`` but read nowhere is worse than
no field: a user edits it, ``pmfa config check`` passes, and nothing changes. This guard walks
every leaf field of the ``Config`` tree, including the records inside lists and dicts such as
``follow.series`` and ``models.slots``, and fails when its name is never read in ``src/pmfa``
outside ``config.py``.

"Read" means an attribute access by that name in Python code (``x.price_move_cents``), found by
parsing the source, so a mention in a comment, a docstring or a string does not count. The check
is by name, not by full path: an unrelated ``.seed`` elsewhere would satisfy
``evaluation.seed``. That makes it a floor, not a proof, but it is enough to catch a knob that
nothing touches at all, which is how dead fields appear.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from pydantic import BaseModel

from pmfa.config import Config, Section

SRC = Path(__file__).resolve().parents[1] / "src" / "pmfa"
CONFIG_MODULE = SRC / "config.py"

# Fields that are legitimately never read by attribute name. Each entry needs a reason.
# Empty today: every field is read by name somewhere.
ALLOWED_UNREAD: dict[str, str] = {}


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """The Pydantic models an annotation holds, directly or inside list, dict or a union."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found += _models_in(arg)
    return found


def leaf_fields(model: type[BaseModel], prefix: str = "") -> list[tuple[str, str]]:
    """Every leaf field under ``model`` as (dotted path, field name)."""
    leaves: list[tuple[str, str]] = []
    for name, info in model.model_fields.items():
        nested = _models_in(info.annotation)
        if nested:
            for sub in nested:
                leaves += leaf_fields(sub, f"{prefix}{name}.")
        else:
            leaves.append((f"{prefix}{name}", name))
    return leaves


def attribute_names_read(root: Path, *, skip: Path) -> set[str]:
    """Names used as ``something.<name>`` in the Python code under ``root``, except ``skip``."""
    names: set[str] = set()
    for file in root.rglob("*.py"):
        if file == skip:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    return names


def unread_fields(model: type[BaseModel], read: set[str]) -> list[str]:
    return [path for path, name in leaf_fields(model) if name not in read]


def test_every_configuration_field_is_read_somewhere():
    read = attribute_names_read(SRC, skip=CONFIG_MODULE)
    unread = [path for path in unread_fields(Config, read) if path not in ALLOWED_UNREAD]
    assert unread == [], f"configuration fields nothing reads: {unread}"


def test_the_allowlist_names_only_real_fields():
    paths = {path for path, _ in leaf_fields(Config)}
    assert set(ALLOWED_UNREAD) <= paths


def test_the_guard_finds_a_dead_field_at_any_depth():
    class Inner(Section):
        used_knob: int = 1
        dead_inner_knob: int = 2

    class Probe(Section):
        dead_top_knob: int = 0
        items: list[Inner] = []
        by_name: dict[str, Inner] = {}

    found = unread_fields(Probe, read={"used_knob"})
    assert found == [
        "dead_top_knob",
        "items.dead_inner_knob",
        "by_name.dead_inner_knob",
    ]


def test_a_mention_in_a_comment_or_docstring_is_not_a_read(tmp_path):
    (tmp_path / "mod.py").write_text(
        '"""Uses ghost_knob."""\n'
        "# ghost_knob is mentioned here\n"
        "def f(config):\n"
        '    """Reads ghost_knob."""\n'
        '    return "ghost_knob", config.real_knob\n',
        encoding="utf-8",
    )
    read = attribute_names_read(tmp_path, skip=tmp_path / "config.py")
    assert "real_knob" in read and "ghost_knob" not in read
