"""The committed sample episodes: finding them, replaying them, and checking them.

Shared by ``pmfa replay``, ``scripts/build_examples.py`` and the tests. An
example is a directory under ``examples/episodes/`` laid out by
``pmfa.world.recording.save_example``::

    world.json               the inputs: trigger, event snapshot, books, documents
    cassette.json            the model records that replay it
    expected/episode.json    the outputs: trace, forecast, decision, orders
    expected/review_card.json  when a human review happened
    README.md

``replay`` runs one offline: the recorded world, a strict ``CassettePlayer``
(any model input that differs from the recording stops it with
``CassetteMiss``), the episode controller, and the file's scripted human
review when it has one. Nothing here needs an account, a key or the network.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pmfa.config import Config, default_config
from pmfa.errors import BudgetExhausted, ModelOutputError, PmfaError, RunBudgetExhausted
from pmfa.models.cassette import Cassette, CassetteEntry, first_difference
from pmfa.ports import Models, World
from pmfa.records.base import Record, RunMode
from pmfa.records.decision import ReviewCard, ReviewPolicy
from pmfa.records.episode import EpisodeRecord, EpisodeStatus, LedgerEntry, ModelResult, Usage
from pmfa.records.roles import TASKS, ModelSlot, RoleTask
from pmfa.world.recorded import RecordedWorldFile, load_recorded_world
from pmfa.world.recording import (
    CARD_FILE,
    CASSETTE_FILE,
    EPISODE_FILE,
    EXPECTED_DIR,
    WORLD_FILE,
)

HAND_MADE_NOTICE = (
    "This is a hand-made fixture with hand-written model records. No model produced them: "
    "a person wrote every Monitor, Researcher, Generator and Critic answer to exercise the "
    "pipeline. Nothing in it is model output or evidence of forecasting skill."
)
"""The first thing the replay prints for a hand-made fixture."""


class ExampleError(PmfaError):
    """An example path does not hold what replay needs."""


# --------------------------------------------------------------------------- finding examples


def examples_root() -> Path:
    """``examples/episodes`` in the working directory, else in the source checkout."""
    here = Path.cwd() / "examples" / "episodes"
    if here.is_dir():
        return here
    return Path(__file__).resolve().parents[2] / "examples" / "episodes"


def list_examples(root: Path | None = None) -> list[Path]:
    """Every example directory (one holding a ``world.json``), sorted by name."""
    base = root or examples_root()
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if (p / WORLD_FILE).is_file())


@dataclass(frozen=True)
class ExamplePaths:
    """Where an example's files are. ``directory`` is ``None`` for a bare world file."""

    world: Path
    cassette: Path
    directory: Path | None

    @property
    def name(self) -> str:
        return self.directory.name if self.directory is not None else self.world.stem

    @property
    def expected_episode(self) -> Path | None:
        return None if self.directory is None else self.directory / EXPECTED_DIR / EPISODE_FILE

    @property
    def expected_card(self) -> Path | None:
        return None if self.directory is None else self.directory / EXPECTED_DIR / CARD_FILE


def resolve(path: str | Path) -> ExamplePaths:
    """An example directory, a world file, or the bare name of a committed example."""
    target = Path(path)
    if not target.exists() and not target.is_absolute() and len(target.parts) == 1:
        named = examples_root() / target
        if named.is_dir():
            target = named
    if target.is_dir():
        world = target / WORLD_FILE
        if not world.is_file():
            raise ExampleError(f"{target} has no {WORLD_FILE}")
        cassette = target / CASSETTE_FILE
        directory: Path | None = target
    elif target.is_file():
        world = target
        cassette = target.parent / CASSETTE_FILE
        directory = target.parent if (target.parent / EXPECTED_DIR).is_dir() else None
    else:
        raise ExampleError(f"no example or world file at {path} (try `pmfa replay --list`)")
    if not cassette.is_file():
        raise ExampleError(f"no {CASSETTE_FILE} next to {world}: replay needs the model records")
    return ExamplePaths(world=world, cassette=cassette, directory=directory)


def load_cassette(path: Path) -> Cassette:
    return Cassette.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- recording model calls


class TapeModels:
    """A ``Models`` wrapper that writes every call to a cassette as it happens.

    The build script wraps ``ScriptedModels`` in it, so the cassette holds the
    hand-written answers (``source: scripted``) in the exact form a
    ``CassettePlayer`` replays.
    """

    def __init__(self, models: Models, config: Config | None = None) -> None:
        self.models = models
        self.config = config or default_config()
        self._cassettes: dict[str, Cassette] = {}
        self._lock = threading.Lock()

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        chosen = slot or TASKS[task].default_slot
        assignment = self.config.models.slots[chosen]
        base: dict[str, Any] = {
            "task": task,
            "slot": chosen,
            "role": TASKS[task].role,
            "model": assignment.model,
            "served_model": assignment.model,
            "effort": assignment.effort,
            "source": "scripted",
            "input": payload.model_dump(mode="json"),
        }
        try:
            result = self.models.call(task, payload, episode_id=episode_id, slot=slot)
        except BudgetExhausted as exc:
            self._add(episode_id, base, outcome="budget_exhausted", error=str(exc), calls=0)
            raise
        except RunBudgetExhausted as exc:
            self._add(episode_id, base, outcome="run_budget_exhausted", error=str(exc), calls=0)
            raise
        except ModelOutputError as exc:
            self._add(episode_id, base, outcome="model_output_error", error=str(exc))
            raise
        meta = result.meta
        self._add(
            episode_id,
            {
                **base,
                "slot": meta.slot,
                "role": meta.role,
                "model": meta.model,
                "served_model": meta.served_model,
                "effort": meta.effort,
            },
            output=result.output.model_dump(mode="json"),
        )
        return result

    def _add(self, episode_id: str, fields: dict[str, Any], **more: Any) -> None:
        with self._lock:
            cassette = self._cassettes.setdefault(episode_id, Cassette(episode_id=episode_id))
            cassette.entries.append(CassetteEntry(seq=len(cassette.entries) + 1, **fields, **more))

    def usage(self, episode_id: str) -> Usage:
        return self.models.usage(episode_id)

    def cassette(self, episode_id: str) -> Cassette:
        with self._lock:
            return self._cassettes.get(episode_id, Cassette(episode_id=episode_id))


# --------------------------------------------------------------------------- running an episode


def review_policy(file: RecordedWorldFile) -> ReviewPolicy:
    """Pause for the file's scripted review when it has one; else fill approve-tier trades tagged."""
    return ReviewPolicy.PAUSE if file.review is not None else ReviewPolicy.FILL_TAGGED


@dataclass
class EpisodeRun:
    """What one episode decided, plus what the printout needs from the world it ran in."""

    episode: EpisodeRecord
    card: ReviewCard | None = None
    ledger: list[LedgerEntry] = field(default_factory=list)


def run_on_world(world: World, file: RecordedWorldFile) -> EpisodeRun:
    """Run the file's trigger on ``world``, applying its scripted review when the episode pauses.

    The review arrives ``after_seconds`` after the pause on the world's
    simulated clock; one that arrives after the proposal's expiry expires it.
    """
    from pmfa.episode import EpisodeController

    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
        if (
            record.status is EpisodeStatus.PENDING_REVIEW
            and file.review is not None
            and record.proposal is not None
        ):
            world.clock.advance(file.review.after_seconds)  # type: ignore[attr-defined]
            review = file.review.review_for(record.proposal.proposal_id, world.clock.now())
            record = controller.resume(record.episode_id, review)
    return EpisodeRun(
        episode=record,
        card=stored_card(world, record),
        ledger=[e for e in world.store.ledger() if e.episode_id == record.episode_id],
    )


def stored_card(world: World, record: EpisodeRecord) -> ReviewCard | None:
    if record.proposal is None:
        return None
    stored = world.store.get_proposal(record.proposal.proposal_id)
    return stored[2] if stored is not None else None


@dataclass
class ReplayResult(EpisodeRun):
    """A replayed example: the run, the files it came from, and what was left unplayed."""

    paths: ExamplePaths | None = None
    file: RecordedWorldFile | None = None
    cassette: Cassette | None = None
    unplayed_calls: int = 0
    unplayed_knowledge: int = 0


def replay(path: str | Path) -> ReplayResult:
    """Replay one example (or world file) offline with its cassette, strictly."""
    from pmfa.models.cassette import CassettePlayer
    from pmfa.world.recorded import RecordedKnowledge, build_recorded_world

    paths = resolve(path)
    file = load_recorded_world(paths.world)
    cassette = load_cassette(paths.cassette)
    player = CassettePlayer({cassette.episode_id: cassette})
    world = build_recorded_world(
        file, models=player, review_policy=review_policy(file), mode=file.mode or RunMode.REPLAY
    )
    run = run_on_world(world, file)
    knowledge = world.knowledge
    return ReplayResult(
        episode=run.episode,
        card=run.card,
        ledger=run.ledger,
        paths=paths,
        file=file,
        cassette=cassette,
        unplayed_calls=player.remaining(cassette.episode_id),
        unplayed_knowledge=knowledge.remaining() if isinstance(knowledge, RecordedKnowledge) else 0,
    )


# --------------------------------------------------------------------------- checking


def check(result: ReplayResult) -> list[str]:
    """Differences between a replay and its example's ``expected/`` files; empty when it matches.

    Whole records are compared, with one field normalised: each model call's
    ``call.source`` (``live``, ``cache``, ``scripted`` or ``cassette``) says where
    that run's answer came from, so a replay always reads ``cassette`` while the
    run that was recorded does not. Nothing else is volatile: the world runs on a
    simulated clock and every id is derived from its inputs.
    """
    paths = result.paths
    if paths is None or paths.directory is None:
        return ["no expected/ directory to compare with"]
    problems: list[str] = []
    expected_episode = paths.expected_episode
    assert expected_episode is not None
    if not expected_episode.is_file():
        problems.append(f"{expected_episode} is missing")
    else:
        recorded = _without_call_source(json.loads(expected_episode.read_text(encoding="utf-8")))
        now = _without_call_source(result.episode.model_dump(mode="json"))
        difference = first_difference(recorded, now)
        if difference is not None:
            problems.append(f"{EXPECTED_DIR}/{EPISODE_FILE} differs at {difference}")
    expected_card = paths.expected_card
    assert expected_card is not None
    if expected_card.is_file():
        recorded_card = json.loads(expected_card.read_text(encoding="utf-8"))
        if result.card is None:
            problems.append(f"{EXPECTED_DIR}/{CARD_FILE} expects a review card; none was made")
        else:
            difference = first_difference(recorded_card, result.card.model_dump(mode="json"))
            if difference is not None:
                problems.append(f"{EXPECTED_DIR}/{CARD_FILE} differs at {difference}")
    elif result.card is not None:
        problems.append(f"a review card was made, but {EXPECTED_DIR}/{CARD_FILE} is missing")
    if result.unplayed_calls:
        problems.append(f"{result.unplayed_calls} recorded model call(s) were never replayed")
    if result.unplayed_knowledge:
        problems.append(
            f"{result.unplayed_knowledge} recorded knowledge call(s) were never replayed"
        )
    return problems


def _without_call_source(data: dict[str, Any]) -> dict[str, Any]:
    """An episode record as JSON with each trace step's ``call.source`` removed (see ``check``)."""
    for step in data.get("trace", []):
        call = step.get("call") if isinstance(step, dict) else None
        if isinstance(call, dict):
            call.pop("source", None)
    return data


def summary_line(directory: Path) -> str:
    """One line for ``pmfa replay --list``: name, exit, and the example's description."""
    file = load_recorded_world(directory / WORLD_FILE)
    exit_name = "?"
    expected = directory / EXPECTED_DIR / EPISODE_FILE
    if expected.is_file():
        data = json.loads(expected.read_text(encoding="utf-8"))
        exit_name = data.get("exit") or data.get("status") or "?"
    return f"{directory.name:<26} {exit_name:<21} {file.description}"


__all__ = [
    "HAND_MADE_NOTICE",
    "EpisodeRun",
    "ExampleError",
    "ExamplePaths",
    "ReplayResult",
    "TapeModels",
    "check",
    "examples_root",
    "list_examples",
    "load_cassette",
    "replay",
    "resolve",
    "review_policy",
    "run_on_world",
    "stored_card",
    "summary_line",
]
