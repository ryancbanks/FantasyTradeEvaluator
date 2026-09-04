"""Portable, validated page-level provenance for FantasyPros ECR captures."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


FANTASYPROS_LATEST_ECR_POLICY = "fantasypros_latest_ecr_v1"


class EcrHorizonEvidence(str, Enum):
    """How the source proved the requested semantic ranking horizon."""

    DIRECT_METADATA = "direct_metadata"
    PRESEASON_REST_OF_SEASON_PAGE = "preseason_rest_of_season_page"


@dataclass(frozen=True, slots=True)
class EcrSourceDetails:
    """Source metadata and atomic visible-page evidence for one position page."""

    ranking_type: str
    type_text: str
    source_week: int
    page_position: str
    source_player_count: int
    source_position_counts: Mapping[str, int]
    expert_selection_policy: str
    expert_group_id: str
    expert_group_title: str
    expert_group_description: str
    page_protocol: str
    page_hostname: str
    page_port: str
    page_path: str
    canonical_protocol: str
    canonical_hostname: str
    canonical_port: str
    canonical_path: str
    document_title: str
    settings_ranking_type: str | None
    settings_position: str | None
    settings_page_heading: str | None
    settings_fallback_note: str | None
    visible_page_heading: str | None
    visible_page_heading_count: int
    visible_ranking_period: str | None
    visible_ranking_period_count: int
    visible_fallback_note: str | None
    visible_fallback_note_count: int
    canonical_link_count: int
    horizon_evidence: EcrHorizonEvidence | str

    def __post_init__(self) -> None:
        for name in (
            "ranking_type",
            "type_text",
            "page_position",
            "document_title",
            "expert_group_id",
            "expert_group_title",
            "expert_group_description",
        ):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        if self.expert_selection_policy != FANTASYPROS_LATEST_ECR_POLICY:
            raise ValueError("expert_selection_policy is unsupported")
        if self.expert_group_id != "default" or self.expert_group_title != "Latest ECR":
            raise ValueError("expert group must identify FantasyPros Latest ECR")
        description = self.expert_group_description.casefold()
        if not all(
            token in description for token in ("accurat", "expert", "recent", "updat")
        ):
            raise ValueError("expert group description does not prove Latest ECR semantics")
        for name in ("page_path", "canonical_path"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        page_protocol = _protocol("page_protocol", self.page_protocol)
        canonical_protocol = _protocol("canonical_protocol", self.canonical_protocol)
        page_hostname = _hostname("page_hostname", self.page_hostname)
        canonical_hostname = _hostname("canonical_hostname", self.canonical_hostname)
        page_port = _port("page_port", self.page_port)
        canonical_port = _port("canonical_port", self.canonical_port)
        if page_hostname != canonical_hostname:
            raise ValueError("canonical hostname must match the captured page")
        for name, value in (
            ("page_protocol", page_protocol),
            ("canonical_protocol", canonical_protocol),
            ("page_hostname", page_hostname),
            ("canonical_hostname", canonical_hostname),
            ("page_port", page_port),
            ("canonical_port", canonical_port),
        ):
            object.__setattr__(self, name, value)
        for name in (
            "settings_ranking_type",
            "settings_position",
            "settings_page_heading",
            "settings_fallback_note",
            "visible_page_heading",
            "visible_ranking_period",
            "visible_fallback_note",
        ):
            object.__setattr__(self, name, _optional_text(name, getattr(self, name)))
        source_week = _integer("source_week", self.source_week, minimum=0, maximum=25)
        source_player_count = _integer(
            "source_player_count", self.source_player_count, minimum=1, maximum=5000
        )
        counts = _position_counts(self.source_position_counts)
        if sum(counts.values()) != source_player_count:
            raise ValueError("source_position_counts must total source_player_count")
        for name in (
            "visible_page_heading_count",
            "visible_ranking_period_count",
            "visible_fallback_note_count",
            "canonical_link_count",
        ):
            count = _integer(name, getattr(self, name), minimum=0, maximum=100)
            object.__setattr__(self, name, count)
        for text_name, count_name in (
            ("visible_page_heading", "visible_page_heading_count"),
            ("visible_ranking_period", "visible_ranking_period_count"),
            ("visible_fallback_note", "visible_fallback_note_count"),
        ):
            if (getattr(self, text_name) is None) != (getattr(self, count_name) == 0):
                raise ValueError(f"{text_name} must agree with {count_name}")
        for name in ("page_path", "canonical_path"):
            path = getattr(self, name)
            if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
                raise ValueError(f"{name} must be a safe absolute path")
        try:
            evidence = EcrHorizonEvidence(self.horizon_evidence)
        except (TypeError, ValueError):
            raise ValueError("horizon_evidence is invalid") from None
        object.__setattr__(self, "source_week", source_week)
        object.__setattr__(self, "source_player_count", source_player_count)
        object.__setattr__(self, "source_position_counts", MappingProxyType(counts))
        object.__setattr__(self, "horizon_evidence", evidence)

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_link_count": self.canonical_link_count,
            "canonical_hostname": self.canonical_hostname,
            "canonical_path": self.canonical_path,
            "canonical_port": self.canonical_port,
            "canonical_protocol": self.canonical_protocol,
            "document_title": self.document_title,
            "expert_group_description": self.expert_group_description,
            "expert_group_id": self.expert_group_id,
            "expert_group_title": self.expert_group_title,
            "expert_selection_policy": self.expert_selection_policy,
            "horizon_evidence": self.horizon_evidence.value,
            "page_path": self.page_path,
            "page_hostname": self.page_hostname,
            "page_port": self.page_port,
            "page_protocol": self.page_protocol,
            "page_position": self.page_position,
            "ranking_type": self.ranking_type,
            "settings_fallback_note": self.settings_fallback_note,
            "settings_page_heading": self.settings_page_heading,
            "settings_position": self.settings_position,
            "settings_ranking_type": self.settings_ranking_type,
            "source_player_count": self.source_player_count,
            "source_position_counts": dict(self.source_position_counts),
            "source_week": self.source_week,
            "type_text": self.type_text,
            "visible_fallback_note": self.visible_fallback_note,
            "visible_fallback_note_count": self.visible_fallback_note_count,
            "visible_page_heading": self.visible_page_heading,
            "visible_page_heading_count": self.visible_page_heading_count,
            "visible_ranking_period": self.visible_ranking_period,
            "visible_ranking_period_count": self.visible_ranking_period_count,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EcrSourceDetails":
        fields = {
            "canonical_link_count",
            "canonical_hostname",
            "canonical_path",
            "canonical_port",
            "canonical_protocol",
            "document_title",
            "expert_group_description",
            "expert_group_id",
            "expert_group_title",
            "expert_selection_policy",
            "horizon_evidence",
            "page_path",
            "page_hostname",
            "page_port",
            "page_protocol",
            "page_position",
            "ranking_type",
            "settings_fallback_note",
            "settings_page_heading",
            "settings_position",
            "settings_ranking_type",
            "source_player_count",
            "source_position_counts",
            "source_week",
            "type_text",
            "visible_fallback_note",
            "visible_fallback_note_count",
            "visible_page_heading",
            "visible_page_heading_count",
            "visible_ranking_period",
            "visible_ranking_period_count",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("ECR source details fields are invalid")
        return cls(**{name: record[name] for name in fields})


def _position_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("source_position_counts must be a non-empty mapping")
    result = {}
    for raw_position, raw_count in value.items():
        position = _text("source_position_counts position", raw_position).upper()
        if position in result:
            raise ValueError("source_position_counts contains a duplicate position")
        result[position] = _integer(
            "source_position_counts count", raw_count, minimum=1, maximum=5000
        )
    return dict(sorted(result.items()))


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _protocol(name: str, value: object) -> str:
    if value != "https:":
        raise ValueError(f"{name} must be https:")
    return value


def _hostname(name: str, value: object) -> str:
    hostname = _text(name, value).casefold().rstrip(".")
    if hostname not in {"fantasypros.com", "www.fantasypros.com"}:
        raise ValueError(f"{name} must be FantasyPros")
    return hostname


def _port(name: str, value: object) -> str:
    if value not in {"", "443"}:
        raise ValueError(f"{name} must be empty or 443")
    return value


def _optional_text(name: str, value: object) -> str | None:
    return None if value is None else _text(name, value)


def _integer(name: str, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")
    return value


__all__ = (
    "EcrHorizonEvidence",
    "EcrSourceDetails",
    "FANTASYPROS_LATEST_ECR_POLICY",
)
