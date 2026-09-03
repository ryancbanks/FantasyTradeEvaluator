"""Safe JSON persistence for corpora, deployable brains, and checkpoints."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
from types import MappingProxyType
from uuid import uuid4

from ._scenario_random import content_id
from .draft_brain import DraftBrain
from .draft_config import DraftLeagueConfig
from .draft_history import DraftPlayerBoard, HistoricalCorpus


_CORPUS_ID = re.compile(r"^draft_corpus_[0-9a-f]{64}$")
_BOARD_ID = re.compile(r"^draft_board_[0-9a-f]{64}$")
_MODEL_ID = re.compile(r"^draft_model_[0-9a-f]{64}$")
_CHECKPOINT_NAME = re.compile(r"^[0-9a-f]{32}\.draftbrain-checkpoint\.json$")
_CHECKPOINT_SUMMARY_NAME = re.compile(
    r"^[0-9a-f]{32}\.draftbrain-checkpoint-summary\.json$"
)
_MAX_MODEL_BYTES = 16 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 512 * 1024 * 1024
_MAX_CORPUS_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DraftModelArtifact:
    brain: DraftBrain
    league_config: DraftLeagueConfig
    corpus_id: str
    trained_seasons: tuple[int, ...]
    generation: int
    metrics: Mapping[str, float]
    created_at: str
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.brain, DraftBrain) or not isinstance(
            self.league_config, DraftLeagueConfig
        ):
            raise ValueError("brain and league_config use invalid types")
        if self.brain.league_config_fingerprint != self.league_config.config_id:
            raise ValueError("brain does not match the saved league configuration")
        if not isinstance(self.corpus_id, str) or not _CORPUS_ID.fullmatch(self.corpus_id):
            raise ValueError("corpus_id is invalid")
        seasons = tuple(self.trained_seasons)
        if not seasons or tuple(sorted(set(seasons))) != seasons:
            raise ValueError("trained_seasons must be unique and increasing")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        metrics = _metrics(self.metrics)
        _timestamp(self.created_at)
        object.__setattr__(self, "trained_seasons", seasons)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "model_id", content_id("draft_model", self._content()))

    def _content(self) -> dict[str, object]:
        return {
            "brain": self.brain.to_record(),
            "league_config": self.league_config.to_record(),
            "corpus_id": self.corpus_id,
            "trained_seasons": list(self.trained_seasons),
            "generation": self.generation,
            "metrics": dict(self.metrics),
            "created_at": self.created_at,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "fantasy_draft_model",
            "schema_version": 1,
            **self._content(),
            "model_id": self.model_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "DraftModelArtifact":
        content = {
            "brain", "league_config", "corpus_id", "trained_seasons",
            "generation", "metrics", "created_at",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "kind", "schema_version", "model_id"
        }:
            raise ValueError("draft model fields are invalid")
        if record["kind"] != "fantasy_draft_model" or record["schema_version"] != 1:
            raise ValueError("draft model kind or schema version is invalid")
        if not isinstance(record["brain"], Mapping) or not isinstance(
            record["league_config"], Mapping
        ):
            raise ValueError("draft model brain and league config must be objects")
        if not isinstance(record["trained_seasons"], list):
            raise ValueError("trained_seasons must be a JSON array")
        artifact = cls(
            DraftBrain.from_record(record["brain"]),
            DraftLeagueConfig.from_record(record["league_config"]),
            record["corpus_id"],
            tuple(record["trained_seasons"]),
            record["generation"],
            record["metrics"],
            record["created_at"],
        )
        if record["model_id"] != artifact.model_id:
            raise ValueError("draft model content does not match model_id")
        return artifact

    def summary(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "brain_id": self.brain.brain_id,
            "league_name": self.league_config.name,
            "config_id": self.league_config.config_id,
            "corpus_id": self.corpus_id,
            "trained_seasons": list(self.trained_seasons),
            "generation": self.generation,
            "metrics": dict(self.metrics),
            "created_at": self.created_at,
        }


class DraftFileStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()
        self.corpus_directory = self.root / "draft-corpora"
        self.model_directory = self.root / "draft-models"
        self.board_directory = self.root / "draft-boards"
        self.checkpoint_directory = self.root / "draft-checkpoints"
        for directory in (
            self.corpus_directory, self.board_directory, self.model_directory,
            self.checkpoint_directory
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def import_corpus(self, record: Mapping[str, object]) -> dict[str, object]:
        corpus = HistoricalCorpus.from_record(record)
        _write_json_atomic(
            self.corpus_directory / f"{corpus.corpus_id}.draftcorpus.json",
            corpus.to_record(),
        )
        summary = corpus.summary()
        _write_json_atomic(
            self.corpus_directory / f"{corpus.corpus_id}.draftcorpus-summary.json",
            {"kind": "draft_corpus_summary", "schema_version": 1, **summary},
        )
        return summary

    def load_corpus(self, corpus_id: str) -> HistoricalCorpus:
        if not isinstance(corpus_id, str) or not _CORPUS_ID.fullmatch(corpus_id):
            raise ValueError("corpus_id is invalid")
        path = self.corpus_directory / f"{corpus_id}.draftcorpus.json"
        return HistoricalCorpus.from_record(_read_json(path, _MAX_CORPUS_BYTES))

    def list_corpora(self) -> tuple[dict[str, object], ...]:
        return self._list(
            self.corpus_directory.glob("*.draftcorpus-summary.json"),
            lambda path: _asset_summary(
                path, _read_json(path, 1024 * 1024),
                "draft_corpus_summary", "corpus_id", _CORPUS_ID,
            ),
        )

    def import_board(self, record: Mapping[str, object]) -> dict[str, object]:
        board = DraftPlayerBoard.from_record(record)
        _write_json_atomic(
            self.board_directory / f"{board.board_id}.draftboard.json",
            board.to_record(),
        )
        summary = board.summary()
        _write_json_atomic(
            self.board_directory / f"{board.board_id}.draftboard-summary.json",
            {"kind": "draft_board_summary", "schema_version": 1, **summary},
        )
        return summary

    def load_board(self, board_id: str) -> DraftPlayerBoard:
        if not isinstance(board_id, str) or not _BOARD_ID.fullmatch(board_id):
            raise ValueError("board_id is invalid")
        return DraftPlayerBoard.from_record(_read_json(
            self.board_directory / f"{board_id}.draftboard.json", _MAX_CORPUS_BYTES
        ))

    def list_boards(self) -> tuple[dict[str, object], ...]:
        return self._list(
            self.board_directory.glob("*.draftboard-summary.json"),
            lambda path: _asset_summary(
                path, _read_json(path, 1024 * 1024),
                "draft_board_summary", "board_id", _BOARD_ID,
            ),
        )

    def save_model(self, artifact: DraftModelArtifact) -> Path:
        if not isinstance(artifact, DraftModelArtifact):
            raise ValueError("artifact must be a DraftModelArtifact")
        path = self.model_directory / f"{artifact.model_id}.draftbrain.json"
        _write_json_atomic(path, artifact.to_record())
        _write_json_atomic(
            self.model_directory / f"{artifact.model_id}.draftbrain-summary.json",
            {"kind": "draft_model_summary", "schema_version": 1, **artifact.summary()},
        )
        return path

    def import_model(self, record: Mapping[str, object]) -> dict[str, object]:
        artifact = DraftModelArtifact.from_record(record)
        self.save_model(artifact)
        return artifact.summary()

    def load_model(self, model_id: str) -> DraftModelArtifact:
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            raise ValueError("model_id is invalid")
        path = self.model_directory / f"{model_id}.draftbrain.json"
        return DraftModelArtifact.from_record(_read_json(path, _MAX_MODEL_BYTES))

    def list_models(self) -> tuple[dict[str, object], ...]:
        return self._list(
            self.model_directory.glob("*.draftbrain-summary.json"),
            lambda path: _asset_summary(
                path, _read_json(path, 1024 * 1024),
                "draft_model_summary", "model_id", _MODEL_ID,
            ),
        )

    def model_path(self, model_id: str) -> Path:
        artifact = self.load_model(model_id)
        path = (self.model_directory / f"{artifact.model_id}.draftbrain.json").resolve()
        if path.parent != self.model_directory or not path.is_file():
            raise FileNotFoundError(model_id)
        return path

    def save_checkpoint(self, job_id: str, record: Mapping[str, object]) -> Path:
        filename = f"{job_id}.draftbrain-checkpoint.json"
        if not _CHECKPOINT_NAME.fullmatch(filename):
            raise ValueError("checkpoint job ID is invalid")
        path = self.checkpoint_directory / filename
        _write_json_atomic(path, record)
        summary = _checkpoint_summary(job_id, record)
        if summary is not None:
            _write_json_atomic(
                self.checkpoint_directory
                / f"{job_id}.draftbrain-checkpoint-summary.json",
                summary,
            )
        return path

    def load_checkpoint(self, job_id: str) -> Mapping[str, object]:
        filename = f"{job_id}.draftbrain-checkpoint.json"
        if not _CHECKPOINT_NAME.fullmatch(filename):
            raise ValueError("checkpoint job ID is invalid")
        return _read_json(self.checkpoint_directory / filename, _MAX_CHECKPOINT_BYTES)

    def list_checkpoints(self) -> tuple[dict[str, object], ...]:
        return self._list(
            self.checkpoint_directory.glob("*.draftbrain-checkpoint-summary.json"),
            lambda path: _validated_checkpoint_summary(
                path, _read_json(path, 64 * 1024)
            ),
        )

    @staticmethod
    def _list(paths, reader) -> tuple[dict[str, object], ...]:
        records = []
        for path in sorted(paths):
            try:
                records.append(reader(path))
            except (OSError, ValueError) as error:
                records.append({"status": "invalid", "file": path.name, "error": str(error)})
        return tuple(records)


def _write_json_atomic(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _checkpoint_summary(job_id: str, record: Mapping[str, object]):
    """Build the small catalog row that keeps large autosaves cheap to list."""

    try:
        evolution = record["evolution_config"]
        league = record["league_config"]
        champion = record.get("champion", record.get("champion_genome"))
        performance = record["champion_performance"]
        if (
            record["kind"] != "draft_training_checkpoint"
            or not all(
                isinstance(value, Mapping)
                for value in (evolution, league, champion, performance)
            )
        ):
            return None
        return {
            "kind": "draft_checkpoint_summary",
            "schema_version": 1,
            "checkpoint_job_id": job_id,
            "checkpoint_id": record["checkpoint_id"],
            "generation_completed": record["generation_completed"],
            "generation_count": evolution["generations"],
            "population_size": evolution["population_size"],
            "training_years": evolution["training_years"],
            "corpus_id": record["corpus_id"],
            "league_name": league["name"],
            "config_id": league["config_id"],
            "champion_brain_id": champion["brain_id"],
            "champion_fitness": performance["fitness"],
        }
    except (KeyError, TypeError):
        return None


def _validated_checkpoint_summary(path: Path, record: Mapping[str, object]):
    keys = {
        "kind", "schema_version", "checkpoint_job_id", "checkpoint_id",
        "generation_completed", "generation_count", "population_size",
        "training_years", "corpus_id", "league_name", "config_id",
        "champion_brain_id", "champion_fitness",
    }
    if set(record) != keys or record["kind"] != "draft_checkpoint_summary" or record[
        "schema_version"
    ] != 1:
        raise ValueError("draft checkpoint summary fields are invalid")
    match = _CHECKPOINT_SUMMARY_NAME.fullmatch(path.name)
    if match is None or path.name.split(".", 1)[0] != record["checkpoint_job_id"]:
        raise ValueError("draft checkpoint summary filename is invalid")
    _require_summary_asset(path)
    if not isinstance(record["training_years"], list):
        raise ValueError("draft checkpoint training years must be an array")
    return dict(record)


def _asset_summary(path, record, kind, id_field, pattern):
    if record.get("kind") != kind or record.get("schema_version") != 1:
        raise ValueError("draft asset summary kind or version is invalid")
    identifier = record.get(id_field)
    if not isinstance(identifier, str) or not pattern.fullmatch(identifier):
        raise ValueError("draft asset summary identifier is invalid")
    if not path.name.startswith(f"{identifier}."):
        raise ValueError("draft asset summary filename does not match its identifier")
    _require_summary_asset(path)
    return {
        key: value for key, value in record.items()
        if key not in {"kind", "schema_version"}
    }


def _require_summary_asset(summary_path: Path) -> None:
    asset_path = summary_path.with_name(
        summary_path.name.replace("-summary.json", ".json")
    )
    if asset_path == summary_path or not asset_path.is_file():
        raise ValueError("draft summary has no matching saved asset")


def _read_json(path: Path, maximum: int) -> Mapping[str, object]:
    try:
        if path.stat().st_size > maximum:
            raise ValueError("saved draft file exceeds its size limit")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value!r}")
            ),
            object_pairs_hook=_unique_object,
        )
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read saved draft file: {error}") from None
    if not isinstance(value, Mapping):
        raise ValueError("saved draft file must contain a JSON object")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _metrics(value: object) -> MappingProxyType:
    if not isinstance(value, Mapping):
        raise ValueError("metrics must be an object")
    result = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("metric names must be non-empty strings")
        if isinstance(raw, bool) or not isinstance(raw, Real) or not math.isfinite(float(raw)):
            raise ValueError(f"metric {key!r} must be finite")
        result[key] = float(raw)
    return MappingProxyType(dict(sorted(result.items())))


def _timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("created_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("created_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a timezone")


__all__ = ("DraftFileStore", "DraftModelArtifact")
