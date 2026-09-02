"""Output invariants for canonicalized host-league inputs."""


def validate_normalized_inputs(value) -> None:
    team_ids = {team.team_id for team in value.league_state.teams}
    roster_ids = [row.team_id for row in value.rosters]
    if len(set(roster_ids)) != len(roster_ids) or set(roster_ids) != team_ids:
        raise ValueError("normalized rosters must exactly cover league teams")
    if any(
        row.roster_cap != value.league_state.roster_rules.roster_cap
        or row.reserve_slot_counts
        != value.league_state.roster_rules.reserve_slot_counts
        for row in value.rosters
    ):
        raise ValueError("normalized roster capacities must match league rules")
    player_ids = {row.canonical_player_id for row in value.player_identities}
    owned_ids = [player_id for roster in value.rosters for player_id in roster.player_ids]
    if len(set(owned_ids)) != len(owned_ids) or set(owned_ids) != player_ids:
        raise ValueError("normalized player identities must exactly cover rostered players")
    eligibility_ids = [row.canonical_player_id for row in value.eligibilities]
    if len(set(eligibility_ids)) != len(eligibility_ids) or set(eligibility_ids) != player_ids:
        raise ValueError("normalized eligibilities must exactly cover rostered players")
    _unique_mapping_rows(
        "player", value.player_provider_ids,
        lambda row: (row.provider, row.provider_player_id),
        lambda row: (row.canonical_player_id, row.provider),
    )
    _unique_mapping_rows(
        "team", value.team_provider_ids,
        lambda row: (row.provider, row.provider_team_id),
        lambda row: (row.canonical_team_id, row.provider),
    )
    mapped_players = {row.canonical_player_id for row in value.player_provider_ids}
    mapped_teams = {row.canonical_team_id for row in value.team_provider_ids}
    if mapped_players != player_ids or mapped_teams != team_ids:
        raise ValueError("provider ID mappings must exactly cover normalized entities")
    _validate_player_mapping_evidence(value)
    if value.completed_history_available and not value.league_state.completed_history_is_usable:
        raise ValueError("available completed history must be complete and standings-consistent")


def _validate_player_mapping_evidence(value) -> None:
    expected = {
        (
            player.canonical_player_id,
            reference.provider,
            reference.provider_player_id,
        )
        for player in value.player_identities
        for reference in player.provider_references
    }
    actual = {
        (row.canonical_player_id, row.provider, row.provider_player_id)
        for row in value.player_provider_ids
    }
    if actual != expected:
        raise ValueError("player provider ID mappings must match identity evidence exactly")


def _unique_mapping_rows(name, rows, reference_key, entity_provider_key) -> None:
    reference_keys = [reference_key(row) for row in rows]
    entity_keys = [entity_provider_key(row) for row in rows]
    if len(set(reference_keys)) != len(reference_keys):
        raise ValueError(f"normalized {name} provider IDs must be globally unique")
    if len(set(entity_keys)) != len(entity_keys):
        raise ValueError(f"normalized {name} can have only one ID per provider")


__all__ = ("validate_normalized_inputs",)
