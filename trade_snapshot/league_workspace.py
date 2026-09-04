"""Application boundary for league profiles, weekly engines, and local workspaces."""

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ._app_support import BUNDLE_ID_PATTERN, boolean, bundle_summary
from .bundle_summary_cache import (
    BundleSummaryCacheError,
    load_cached_bundle_summary,
    save_bundle_with_summary,
    save_cached_bundle_summary,
)
from .engine_bundle import EngineBundle, load_engine_bundle
from .league_catalog import LeagueCatalog
from .weekly_collection import WeeklyCollectionRequest

UNASSIGNED_PROFILE_ID = "unassigned"
_PROFILE_FIELDS = frozenset({
    "name",
    "season",
    "scoring",
    "host_league_url",
    "yahoo_projection_league_url",
})
_COLLECTION_REQUIRED_FIELDS = frozenset({
    "week",
    "include_future_weekly",
    "allow_surrogate_power",
})
_COLLECTION_OPTIONAL_FIELDS = frozenset({
    "use_fantasypros",
    "use_broad_consensus",
    "refresh_public_player_data",
})


@dataclass(frozen=True, slots=True)
class LeagueCollectionPlan:
    request: WeeklyCollectionRequest
    workspace: Path
    espn_league_id: str | None


class LeagueWorkspaceService:
    """Keep private league configuration outside immutable portable bundles."""

    def __init__(self, data_directory: str | Path, bundle_directory: str | Path) -> None:
        self.data_directory = Path(data_directory).resolve()
        self.bundle_directory = Path(bundle_directory).resolve()
        self.workspace_directory = self.data_directory / "leagues"
        self.workspace_directory.mkdir(parents=True, exist_ok=True)
        self.bundle_directory.mkdir(parents=True, exist_ok=True)
        self.catalog = LeagueCatalog(self.data_directory / "league-catalog.sqlite3")

    def create_profile(self, payload: Mapping[str, object]) -> dict[str, object]:
        values = _profile_payload(payload, partial=False)
        return self.catalog.create_profile(
            values["name"],
            values["season"],
            values["scoring"],
            espn_league_url=values["host_league_url"],
            yahoo_league_url=values["yahoo_projection_league_url"],
        ).to_record()

    def update_profile(
        self, profile_id: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        values = _profile_payload(payload, partial=True)
        arguments = {}
        for source, target in (
            ("name", "name"),
            ("season", "season"),
            ("scoring", "scoring"),
            ("host_league_url", "espn_league_url"),
            ("yahoo_projection_league_url", "yahoo_league_url"),
        ):
            if source in values:
                arguments[target] = values[source]
        return self.catalog.update_profile(profile_id, **arguments).to_record()

    def archive_profile(self, profile_id: str) -> dict[str, object]:
        return self.catalog.archive_profile(profile_id).to_record()

    def restore_profile(self, profile_id: str) -> dict[str, object]:
        return self.catalog.restore_profile(profile_id).to_record()

    def profile(self, profile_id: str) -> dict[str, object]:
        return self.catalog.get_profile(profile_id).to_record()

    def profiles(
        self,
        *,
        include_archived: bool,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        record = self.catalog.list_profiles(
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        ).to_record()
        # The browser walks every profile page. Scanning bundle files and all
        # associations once per page would turn that refresh into O(pages ×
        # bundles), so publish this catalog-wide value on the first page only.
        if cursor is None:
            record["unassigned_bundle_count"] = len(self.unassigned_bundle_ids())
        return record

    def bundle_rows(self, profile_id: str) -> tuple[dict[str, object], ...]:
        if profile_id == UNASSIGNED_PROFILE_ID:
            rows = []
            for bundle_id in self.unassigned_bundle_ids():
                path = self.bundle_directory / f"{bundle_id}.json"
                try:
                    try:
                        summary = load_cached_bundle_summary(path)
                    except BundleSummaryCacheError:
                        summary = None
                    if summary is None:
                        bundle = load_engine_bundle(path)
                        summary = bundle_summary(bundle)
                        try:
                            save_cached_bundle_summary(bundle, path)
                        except BundleSummaryCacheError:
                            pass
                    rows.append(_lean_bundle(summary))
                except ValueError as error:
                    rows.append({
                        "file": path.name,
                        "status": "invalid",
                        "error": str(error),
                    })
            return tuple(rows)
        owner = self.catalog.get_profile(profile_id)
        rows = []
        for association in self.catalog.list_bundle_associations(profile_id):
            record = association.to_record()
            path = self.bundle_directory / f"{association.bundle_id}.json"
            try:
                bundle = load_engine_bundle(path)
                actual = (
                    bundle.bundle_id,
                    bundle.state.season,
                    bundle.state.first_remaining_week,
                    len(bundle.state.teams),
                    bundle.methodology_mode,
                )
                expected = (
                    association.bundle_id,
                    association.season,
                    association.week,
                    association.team_count,
                    association.power_engine_mode,
                )
                if actual != expected:
                    raise ValueError(
                        "The weekly bundle no longer matches its league catalog record."
                    )
                if _bundle_scoring(bundle) != owner.scoring:
                    raise ValueError(
                        "The weekly bundle no longer matches the league scoring."
                    )
                record["status"] = "ready"
            except (OSError, ValueError):
                record.update(
                    status="invalid",
                    error=(
                        "The associated weekly bundle is missing, damaged, or no longer "
                        "matches this league."
                    ),
                )
            rows.append(record)
        return tuple(rows)

    def full_bundle(self, bundle_id: str) -> dict[str, object]:
        return bundle_summary(load_engine_bundle(self._bundle_path(bundle_id)))

    def import_bundle(
        self,
        record: Mapping[str, object],
        *,
        profile_id: str | None = None,
    ) -> dict[str, object]:
        bundle = EngineBundle.from_record(record)
        if profile_id is not None:
            owner = self.catalog.get_profile(profile_id)
            if owner.archived:
                raise ValueError("restore the league profile before adding a bundle")
            if owner.season != bundle.state.season:
                raise ValueError("bundle season must match the league profile season")
            _require_matching_scoring(owner.scoring, bundle)
            existing = self.catalog.bundle_association(bundle.bundle_id)
            if existing is not None and existing.profile_id != profile_id:
                raise ValueError("bundle is already associated with another league")
        save_bundle_with_summary(
            bundle, self.bundle_directory / f"{bundle.bundle_id}.json"
        )
        if profile_id is not None:
            self.associate_bundle(profile_id, bundle)
        return bundle_summary(bundle)

    def assign_bundle(self, profile_id: str, bundle_id: str) -> dict[str, object]:
        bundle = load_engine_bundle(self._bundle_path(bundle_id))
        association = self.associate_bundle(profile_id, bundle)
        return association.to_record()

    def save_my_team(
        self,
        profile_id: str,
        *,
        bundle_id: str,
        team_id: object,
    ) -> dict[str, object]:
        association = self.catalog.bundle_association(bundle_id)
        if association is None or association.profile_id != profile_id:
            raise ValueError("the selected weekly bundle does not belong to this league")
        bundle = load_engine_bundle(self._bundle_path(bundle_id))
        if (
            not isinstance(team_id, str)
            or team_id != team_id.strip()
            or not 1 <= len(team_id) <= 200
            or not team_id.isprintable()
        ):
            raise ValueError("team_id must be 1 to 200 printable characters")
        team_ids = {team.team_id for team in bundle.state.teams}
        if team_id not in team_ids:
            raise ValueError("team_id is not present in the selected weekly bundle")
        return self.catalog.save_my_team(profile_id, team_id).to_record()

    def collection_plan(
        self,
        profile_id: str,
        payload: Mapping[str, object],
    ) -> LeagueCollectionPlan:
        fields = set(payload) if isinstance(payload, Mapping) else set()
        if (
            not isinstance(payload, Mapping)
            or not _COLLECTION_REQUIRED_FIELDS <= fields
            or not fields <= _COLLECTION_REQUIRED_FIELDS | _COLLECTION_OPTIONAL_FIELDS
        ):
            raise ValueError("league weekly collection fields are invalid")
        profile = self.catalog.get_profile(profile_id)
        if profile.archived:
            raise ValueError("restore this league before collecting a new week")
        use_fantasypros = boolean(
            "use_fantasypros", payload.get("use_fantasypros", True)
        )
        if profile.yahoo_collection_url is None:
            raise ValueError("this league needs a Yahoo projection connection")
        if not use_fantasypros and profile.espn_collection_url is None:
            raise ValueError(
                "this league needs an ESPN connection when FantasyPros is turned off"
            )
        request = WeeklyCollectionRequest(
            season=profile.season,
            week=payload["week"],
            scoring=profile.scoring,
            host_league_url=profile.espn_collection_url,
            yahoo_projection_league_url=profile.yahoo_collection_url,
            include_future_weekly=boolean(
                "include_future_weekly", payload["include_future_weekly"]
            ),
            allow_surrogate_power=boolean(
                "allow_surrogate_power", payload["allow_surrogate_power"]
            ),
            use_fantasypros=use_fantasypros,
            use_broad_consensus=boolean(
                "use_broad_consensus", payload.get("use_broad_consensus", False)
            ),
            refresh_public_player_data=boolean(
                "refresh_public_player_data",
                payload.get("refresh_public_player_data", False),
            ),
        )
        workspace = (self.workspace_directory / profile.profile_id).resolve()
        if workspace.parent != self.workspace_directory:
            raise ValueError("league workspace path is invalid")
        workspace.mkdir(parents=True, exist_ok=True)
        self._seed_legacy_formula(workspace)
        return LeagueCollectionPlan(
            request=request,
            workspace=workspace,
            espn_league_id=profile.espn_league_id,
        )

    def associate_bundle(
        self,
        profile_id: str,
        bundle: EngineBundle,
        *,
        expected_espn_league_id: str | None = None,
    ):
        if not isinstance(bundle, EngineBundle):
            raise ValueError("bundle must be an EngineBundle")
        scoring = _bundle_scoring(bundle)
        return self.catalog.associate_bundle(
            profile_id,
            bundle_id=bundle.bundle_id,
            season=bundle.state.season,
            week=bundle.state.first_remaining_week,
            team_count=len(bundle.state.teams),
            power_engine_mode=bundle.methodology_mode,
            scoring=scoring,
            expected_espn_league_id=expected_espn_league_id,
        )

    def unassigned_bundle_ids(self) -> tuple[str, ...]:
        associated = self.catalog.associated_bundle_ids()
        return tuple(sorted(
            path.stem
            for path in self.bundle_directory.glob("engine_*.json")
            if BUNDLE_ID_PATTERN.fullmatch(path.stem) and path.stem not in associated
        ))

    def _bundle_path(self, bundle_id: str) -> Path:
        if not isinstance(bundle_id, str) or not BUNDLE_ID_PATTERN.fullmatch(bundle_id):
            raise ValueError("bundle_id is invalid")
        path = self.bundle_directory / f"{bundle_id}.json"
        if not path.is_file():
            raise FileNotFoundError(bundle_id)
        return path

    def _seed_legacy_formula(self, workspace: Path) -> None:
        source = self.data_directory / "methodology" / "strength-formula.json"
        target = workspace / "methodology" / "strength-formula.json"
        if not source.is_file() or target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def _profile_payload(
    payload: Mapping[str, object], *, partial: bool
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("league profile must be an object")
    keys = set(payload)
    if not keys <= _PROFILE_FIELDS or (not partial and keys != _PROFILE_FIELDS):
        raise ValueError("league profile fields are invalid")
    if partial and not keys:
        raise ValueError("at least one league profile field must be updated")
    return dict(payload)


def _lean_bundle(summary: Mapping[str, object]) -> dict[str, object]:
    return {
        key: summary[key]
        for key in (
            "bundle_id",
            "status",
            "season",
            "week",
            "team_count",
            "power_engine_mode",
        )
    }


def _bundle_scoring(bundle: EngineBundle) -> str:
    settings = bundle.scoring_profile.settings
    scoring_settings = settings.get("scoring_settings")
    if isinstance(scoring_settings, Mapping):
        rank_type = scoring_settings.get("playerRankType")
        scoring_by_rank_type = {
            "STANDARD": "STD",
            "HALF_PPR": "HALF",
            "PPR": "PPR",
        }
        if rank_type in scoring_by_rank_type:
            return scoring_by_rank_type[rank_type]
        raise ValueError(
            "bundle ESPN playerRankType must be STANDARD, HALF_PPR, or PPR"
        )
    reception = settings.get("reception")
    receiving = settings.get("receiving")
    if reception is None and isinstance(receiving, Mapping):
        reception = receiving.get("reception")
    if isinstance(reception, bool) or not isinstance(reception, (int, float)):
        raise ValueError("bundle scoring profile is missing reception scoring")
    scoring_by_reception = {0.0: "STD", 0.5: "HALF", 1.0: "PPR"}
    try:
        return scoring_by_reception[float(reception)]
    except KeyError:
        raise ValueError(
            "bundle reception scoring must be Standard, Half PPR, or PPR"
        ) from None


def _require_matching_scoring(profile_scoring: str, bundle: EngineBundle) -> None:
    if _bundle_scoring(bundle) != profile_scoring:
        raise ValueError(
            "bundle reception scoring must match the league profile scoring"
        )


__all__ = ("UNASSIGNED_PROFILE_ID", "LeagueWorkspaceService")
