"""Exact-ID materialization from public NFL evidence into portable profiles."""

from collections import defaultdict

from ._scenario_random import content_id, require_json_int, require_text
from .identity import IdentityRegistry, ProviderReference
from .player_profiles import (
    PlayerAvailabilityEvent,
    PlayerAvailabilitySeason,
    PlayerGameStats,
    PlayerProfile,
    PlayerProfileProvenance,
    PlayerProfileSnapshot,
    ProfileMaterializationIssue,
)
from .public_player_data import (
    PublicDataProvenance,
    PublicPlayerDataSnapshot,
)


class PlayerProfileMaterializationError(ValueError):
    """Expected incompatibility while joining valid public profile evidence."""


def materialize_player_profiles(
    *,
    league_snapshot_id: str,
    as_of_week: int,
    identities: IdentityRegistry,
    public_data: PublicPlayerDataSnapshot,
) -> PlayerProfileSnapshot:
    """Join exact IDs into the full catalog at the declared weekly boundary.

    Current-season statistics represent completed games, so only weeks before
    ``as_of_week`` are retained. Current-season injury reports may describe the
    declared week and are retained through it. Prior-season evidence is complete.
    """

    require_text("league_snapshot_id", league_snapshot_id)
    require_json_int("as_of_week", as_of_week, minimum=1)
    if as_of_week > 25:
        raise ValueError("as_of_week must not exceed 25")
    if not isinstance(identities, IdentityRegistry):
        raise ValueError("identities must be an IdentityRegistry")
    if not isinstance(public_data, PublicPlayerDataSnapshot):
        raise ValueError("public_data must be a PublicPlayerDataSnapshot")

    try:
        issues = []
        crosswalk_aliases, crosswalk_references, blocked_crosswalk_ids = (
            _match_crosswalk(identities, public_data.id_crosswalk, issues)
        )
        metadata_by_id, canonical_by_gsis = _match_sleeper_players(
            identities,
            public_data.sleeper_players,
            issues,
            crosswalk_aliases,
            crosswalk_references,
            blocked_crosswalk_ids,
        )
        current = _stats_by_player(
            (
                row
                for row in public_data.current_stats.rows
                if row.week < as_of_week
            ),
            identities,
            canonical_by_gsis,
        )
        previous = _stats_by_player(public_data.previous_stats.rows, identities, canonical_by_gsis)
        injuries, injury_availability = _injuries_by_player(
            getattr(public_data, "injury_history", ()),
            identities,
            canonical_by_gsis,
            current_season=public_data.season,
            as_of_week=as_of_week,
        )
        trends = {row.sleeper_player_id: row for row in public_data.trends}
        identity_by_id = {row.canonical_player_id: row for row in identities.players}
        player_ids = (
            set(identity_by_id)
            | set(metadata_by_id)
            | set(current)
            | set(previous)
            | set(injuries)
        )
        players = tuple(
            _profile(
                player_id,
                identity_by_id.get(player_id),
                metadata_by_id.get(player_id),
                current.get(player_id, ()),
                previous.get(player_id, ()),
                injuries.get(player_id, ()),
                trends,
                crosswalk_references.get(player_id, ()),
            )
            for player_id in player_ids
        )
        return PlayerProfileSnapshot(
            league_snapshot_id=league_snapshot_id,
            season=public_data.season,
            as_of_week=as_of_week,
            captured_at=public_data.captured_at,
            identity_registry_id=content_id("identity-registry", identities.to_record()),
            source_data_id=public_data.data_id,
            current_stats_availability=public_data.current_stats.availability.value,
            previous_stats_availability=public_data.previous_stats.availability.value,
            players=players,
            provenance=tuple(_provenance(row) for row in public_data.provenance),
            injury_history_availability=injury_availability,
            materialization_issues=tuple(issues),
        )
    except ValueError as error:
        raise PlayerProfileMaterializationError(str(error)) from error


def _match_crosswalk(identities, rows, issues):
    aliases = {}
    references = defaultdict(dict)
    blocked = set()
    components_by_canonical = defaultdict(list)
    identity_references = defaultdict(tuple)
    for identity in identities.players:
        identity_references[identity.canonical_player_id] = tuple(
            reference.key for reference in identity.provider_references
        )
    for keys in _crosswalk_components(rows):
        values_by_provider = defaultdict(set)
        for provider, provider_id in keys:
            values_by_provider[provider].add(provider_id)
        contradictory = any(
            len(provider_ids) > 1
            for provider_ids in values_by_provider.values()
        )
        candidates = {
            matched.canonical_player_id
            for provider, provider_id in keys
            if (matched := identities.lookup(provider, provider_id)) is not None
        }
        if contradictory or len(candidates) > 1:
            blocked.update(keys)
            issues.append(
                ProfileMaterializationIssue(
                    "dynastyprocess",
                    _crosswalk_label(keys),
                    (
                        "crosswalk component contains multiple IDs for one provider"
                        if contradictory
                        else "crosswalk references conflicting canonical identities"
                    ),
                )
            )
            continue
        canonical = next(iter(candidates), _crosswalk_canonical(keys))
        components_by_canonical[canonical].append(keys)
    for canonical, components in sorted(components_by_canonical.items()):
        keys = tuple(sorted(key for component in components for key in component))
        values_by_provider = defaultdict(set)
        for provider, provider_id in (*keys, *identity_references[canonical]):
            values_by_provider[provider].add(provider_id)
        if any(len(provider_ids) > 1 for provider_ids in values_by_provider.values()):
            blocked.update(keys)
            for component in components:
                issues.append(
                    ProfileMaterializationIssue(
                        "dynastyprocess",
                        _crosswalk_label(component),
                        (
                            "crosswalk and existing identity resolve one canonical "
                            "player to multiple IDs for one provider"
                        ),
                    )
                )
            continue
        for key in keys:
            aliases[key] = canonical
            references[canonical][key] = ProviderReference(*key)
    return (
        aliases,
        {player_id: tuple(values.values()) for player_id, values in references.items()},
        blocked,
    )


def _crosswalk_components(rows):
    graph = defaultdict(set)
    for row in rows:
        keys = tuple(
            (provider, value)
            for provider, value in (
                ("gsis", row.gsis_id),
                ("espn", row.espn_id),
                ("sleeper", row.sleeper_id),
            )
            if value is not None
        )
        for key in keys:
            graph[key].update(value for value in keys if value != key)
    unseen = set(graph)
    components = []
    while unseen:
        pending = [unseen.pop()]
        component = set()
        while pending:
            key = pending.pop()
            if key in component:
                continue
            component.add(key)
            pending.extend(graph[key] - component)
        unseen.difference_update(component)
        components.append(tuple(sorted(component)))
    return tuple(sorted(components))


def _crosswalk_canonical(keys):
    by_provider = dict(keys)
    for provider in ("gsis", "sleeper", "espn"):
        if provider in by_provider:
            return f"{provider}:{by_provider[provider]}"
    raise AssertionError("crosswalk component contained no provider IDs")


def _crosswalk_label(keys):
    return "|".join(
        f"{provider}:{value}"
        for provider, value in sorted(keys)
    )


def _match_sleeper_players(
    identities,
    rows,
    issues,
    crosswalk_aliases,
    crosswalk_references,
    blocked_crosswalk_ids,
):
    metadata_by_id = {}
    defense_by_team = defaultdict(set)
    for identity in identities.players:
        if identity.position == "DST" and identity.nfl_team_id is not None:
            defense_by_team[identity.nfl_team_id.upper()].add(
                identity.canonical_player_id
            )
    canonical_by_gsis = {
        provider_id: canonical
        for (provider, provider_id), canonical in crosswalk_aliases.items()
        if provider == "gsis"
    }
    known_ids = defaultdict(lambda: defaultdict(set))
    for identity in identities.players:
        for reference in identity.provider_references:
            known_ids[identity.canonical_player_id][reference.provider].add(
                reference.provider_player_id
            )
    for canonical, references in crosswalk_references.items():
        for reference in references:
            known_ids[canonical][reference.provider].add(
                reference.provider_player_id
            )
    duplicate_gsis = _duplicate_external_ids(rows, "gsis_id")
    duplicate_espn = _duplicate_external_ids(rows, "espn_id")
    for row in rows:
        keys = tuple(
            (provider, provider_id)
            for provider, provider_id in (
                ("sleeper", row.sleeper_player_id),
                ("espn", row.espn_id),
                ("gsis", row.gsis_id),
            )
            if provider_id is not None
        )
        if any(key in blocked_crosswalk_ids for key in keys):
            continue
        conflicts = []
        if row.gsis_id in duplicate_gsis:
            conflicts.append("duplicate GSIS ID")
        if row.espn_id in duplicate_espn:
            conflicts.append("duplicate ESPN ID")
        if conflicts:
            issues.append(
                ProfileMaterializationIssue(
                    "sleeper", row.sleeper_player_id, ", ".join(conflicts)
                )
            )
            continue
        candidates = set()
        for provider, provider_id in keys:
            alias = crosswalk_aliases.get((provider, provider_id))
            if alias is not None:
                candidates.add(alias)
            matched = identities.lookup(provider, provider_id)
            if matched is not None:
                candidates.add(matched.canonical_player_id)
        defense_team = _team_defense(row)
        if defense_team is not None:
            candidates.update(defense_by_team[defense_team])
        if len(candidates) > 1:
            issues.append(
                ProfileMaterializationIssue(
                    "sleeper",
                    row.sleeper_player_id,
                    "conflicting exact identity references",
                )
            )
            continue
        canonical = next(
            iter(candidates),
            (
                f"defense:{defense_team}"
                if defense_team is not None
                else f"sleeper:{row.sleeper_player_id}"
            ),
        )
        conflicting_providers = sorted(
            provider
            for provider, provider_id in keys
            if (
                known_ids[canonical][provider]
                and (
                    len(known_ids[canonical][provider]) > 1
                    or provider_id not in known_ids[canonical][provider]
                )
            )
        )
        if conflicting_providers:
            issues.append(
                ProfileMaterializationIssue(
                    "sleeper",
                    row.sleeper_player_id,
                    "metadata adds a conflicting exact provider ID: "
                    + ", ".join(conflicting_providers),
                )
            )
            continue
        if canonical in metadata_by_id:
            issues.append(
                ProfileMaterializationIssue(
                    "sleeper",
                    row.sleeper_player_id,
                    "another Sleeper row already maps to this canonical player",
                )
            )
            continue
        metadata_by_id[canonical] = row
        for provider, provider_id in keys:
            known_ids[canonical][provider].add(provider_id)
        if row.gsis_id is not None:
            known = canonical_by_gsis.setdefault(row.gsis_id, canonical)
            if known != canonical:
                raise ValueError("one GSIS player maps to multiple canonical players")
    return metadata_by_id, canonical_by_gsis


def _team_defense(row):
    if (
        row.position in {"DEF", "DST"}
        and row.nfl_team_id is not None
        and row.sleeper_player_id.upper() == row.nfl_team_id.upper()
    ):
        return row.nfl_team_id.upper()
    return None


def _stats_by_player(rows, identities, canonical_by_gsis):
    result = defaultdict(list)
    for row in rows:
        canonical = _canonical_for_gsis(row.gsis_id, identities, canonical_by_gsis)
        result[canonical].append(row)
    return result


def _injuries_by_player(
    seasons,
    identities,
    canonical_by_gsis,
    *,
    current_season,
    as_of_week,
):
    result = defaultdict(list)
    availability = []
    for season in seasons:
        availability.append(
            PlayerAvailabilitySeason(season.season, season.availability.value)
        )
        for row in season.rows:
            if row.season == current_season and row.week > as_of_week:
                continue
            canonical = _canonical_for_gsis(row.gsis_id, identities, canonical_by_gsis)
            result[canonical].append(row)
    return result, tuple(availability)


def _canonical_for_gsis(gsis_id, identities, canonical_by_gsis):
    from_metadata = canonical_by_gsis.get(gsis_id)
    from_registry = identities.lookup("gsis", gsis_id)
    registry_id = None if from_registry is None else from_registry.canonical_player_id
    if from_metadata is not None and registry_id is not None and from_metadata != registry_id:
        raise ValueError("GSIS evidence conflicts with the identity registry")
    return from_metadata or registry_id or f"gsis:{gsis_id}"


def _profile(
    player_id,
    identity,
    metadata,
    current_rows,
    previous_rows,
    injury_rows,
    trends,
    crosswalk_references,
):
    samples = tuple((*current_rows, *previous_rows, *injury_rows))
    name = (
        identity.display_name
        if identity is not None
        else metadata.display_name
        if metadata is not None
        else _sample_text(samples, "display_name")
    )
    position = (
        identity.position
        if identity is not None
        else metadata.position
        if metadata is not None
        else _latest_value(samples, "position")
    )
    team = (
        identity.nfl_team_id
        if identity is not None
        else metadata.nfl_team_id
        if metadata is not None and metadata.nfl_team_id is not None
        else _latest_value(samples, "nfl_team_id")
    )
    references = _references(identity, metadata, samples, crosswalk_references)
    trend = None if metadata is None else trends.get(metadata.sleeper_player_id)
    return PlayerProfile(
        canonical_player_id=player_id,
        display_name=name,
        position=position,
        nfl_team_id=team,
        provider_references=references,
        fantasy_positions=(
            metadata.fantasy_positions
            if metadata is not None and metadata.fantasy_positions
            else (() if position is None else (position,))
        ),
        active=None if metadata is None else metadata.active,
        status=None if metadata is None else metadata.status,
        injury_status=None if metadata is None else metadata.injury_status,
        injury_body_part=None if metadata is None else metadata.injury_body_part,
        practice_participation=(
            None if metadata is None else metadata.practice_participation
        ),
        depth_chart_position=(
            None if metadata is None else metadata.depth_chart_position
        ),
        depth_chart_order=None if metadata is None else metadata.depth_chart_order,
        years_experience=None if metadata is None else metadata.years_experience,
        jersey_number=None if metadata is None else metadata.jersey_number,
        headshot_url=_latest_value(current_rows, "headshot_url")
        or _latest_value(previous_rows, "headshot_url"),
        adds=None if trend is None else trend.adds,
        drops=None if trend is None else trend.drops,
        current_season_stats=tuple(_game_stats(row) for row in current_rows),
        previous_season_stats=tuple(_game_stats(row) for row in previous_rows),
        availability_history=tuple(_availability_event(row) for row in injury_rows),
    )


def _game_stats(row):
    return PlayerGameStats(
        season=row.season,
        week=row.week,
        game_id=row.game_id,
        nfl_team_id=row.nfl_team_id,
        opponent_team_id=row.opponent_team_id,
        fantasy_points_standard=row.fantasy_points_standard,
        fantasy_points_ppr=row.fantasy_points_ppr,
        stat_values=dict(row.stat_values),
    )


def _availability_event(row):
    return PlayerAvailabilityEvent(
        season=row.season,
        week=row.week,
        nfl_team_id=row.nfl_team_id,
        report_primary_injury=row.report_primary_injury,
        report_secondary_injury=row.report_secondary_injury,
        report_status=row.report_status,
        practice_primary_injury=row.practice_primary_injury,
        practice_secondary_injury=row.practice_secondary_injury,
        practice_status=row.practice_status,
        source_modified_at=row.source_modified_at,
    )


def _references(identity, metadata, samples, crosswalk_references):
    references = {row.key: row for row in crosswalk_references}
    if identity is not None:
        references.update({row.key: row for row in identity.provider_references})
    if metadata is not None:
        for provider, provider_id in (
            ("sleeper", metadata.sleeper_player_id),
            ("espn", metadata.espn_id),
            ("gsis", metadata.gsis_id),
        ):
            if provider_id is not None:
                references.setdefault((provider, provider_id), ProviderReference(provider, provider_id))
    if not any(provider == "gsis" for provider, _ in references):
        gsis_ids = {getattr(row, "gsis_id", None) for row in samples}
        gsis_ids.discard(None)
        if len(gsis_ids) == 1:
            gsis_id = next(iter(gsis_ids))
            references[("gsis", gsis_id)] = ProviderReference("gsis", gsis_id)
    return tuple(
        ProviderReference(provider, provider_id)
        for provider, provider_id in references
    )


def _sample_text(rows, attribute):
    value = _latest_value(rows, attribute)
    if value is None:
        raise ValueError(f"public player evidence lacks {attribute}")
    return value


def _latest_value(rows, attribute):
    for row in sorted(rows, key=lambda value: (value.season, value.week), reverse=True):
        value = getattr(row, attribute, None)
        if value is not None:
            return value
    return None


def _provenance(row: PublicDataProvenance) -> PlayerProfileProvenance:
    return PlayerProfileProvenance(
        provider=row.provider,
        dataset=row.dataset,
        source_url=row.requested_url,
        captured_at=row.captured_at,
        source_updated_at=row.source_updated_at,
        etag=row.etag,
        status=row.availability.value,
        content_sha256=row.content_sha256,
        byte_count=row.byte_count,
    )


def _duplicate_external_ids(rows, attribute):
    counts = defaultdict(int)
    for row in rows:
        value = getattr(row, attribute)
        if value is not None:
            counts[value] += 1
    return {value for value, count in counts.items() if count > 1}


__all__ = ("PlayerProfileMaterializationError", "materialize_player_profiles")
