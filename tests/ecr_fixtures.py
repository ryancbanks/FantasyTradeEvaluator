"""Shared complete ECR provenance fixtures for strict domain tests."""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from trade_snapshot.ecr import EcrSourceProvenance
from trade_snapshot.ecr_source import (
    EcrHorizonEvidence,
    EcrSourceDetails,
    FANTASYPROS_LATEST_ECR_POLICY,
)


def ecr_source_details(
    *,
    season=2026,
    week=1,
    horizon="weekly",
    position="RB",
    source_scoring="PPR",
    source_player_count=1,
    source_position_counts=None,
):
    prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
        source_scoring
    ]
    slug = (
        f"ros-{prefix}{position.casefold()}"
        if horizon == "ros"
        else f"{prefix}{position.casefold()}"
    )
    path = f"/nfl/rankings/{slug}.php"
    is_weekly = horizon == "weekly"
    heading = (
        f"Fantasy Football Week {week} Rankings ({season})"
        if is_weekly
        else f"Fantasy Football ROS Rankings ({season})"
    )
    return EcrSourceDetails(
        ranking_type="weekly" if is_weekly else "ros",
        type_text=(
            f"Weekly {source_scoring}"
            if is_weekly
            else f"Rest of Season {source_scoring}"
        ),
        source_week=week,
        page_position=position,
        source_player_count=source_player_count,
        source_position_counts=(
            {position: source_player_count}
            if source_position_counts is None
            else source_position_counts
        ),
        expert_selection_policy=FANTASYPROS_LATEST_ECR_POLICY,
        expert_group_id="default",
        expert_group_title="Latest ECR",
        expert_group_description="More accurate experts with recent updates",
        page_protocol="https:",
        page_hostname="www.fantasypros.com",
        page_port="",
        page_path=path,
        canonical_protocol="https:",
        canonical_hostname="www.fantasypros.com",
        canonical_port="",
        canonical_path=path,
        canonical_link_count=1,
        document_title=(
            f"Fantasy Football Week {week} Rankings | FantasyPros"
            if is_weekly
            else "Rest of Season Rankings | FantasyPros"
        ),
        settings_ranking_type="weekly" if is_weekly else "ros",
        settings_position=position,
        settings_page_heading=heading,
        settings_fallback_note=None,
        visible_page_heading=heading,
        visible_page_heading_count=1,
        visible_ranking_period=f"Week {week}" if is_weekly else "Rest of Season",
        visible_ranking_period_count=1,
        visible_fallback_note=None,
        visible_fallback_note_count=0,
        horizon_evidence=EcrHorizonEvidence.DIRECT_METADATA,
    )


def ecr_source_provenance(
    *,
    captured_at=None,
    source_updated_at=None,
    league_scoring="PPR",
    source_scoring="PPR",
    **details,
):
    captured = captured_at or datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
    return EcrSourceProvenance(
        league_scoring=league_scoring,
        source_scoring=source_scoring,
        capture_method="visible_page",
        captured_at=captured,
        source_updated_at=source_updated_at,
        source_updated_text="Updated today",
        source_details=ecr_source_details(
            source_scoring=source_scoring,
            **details,
        ),
    )


def preseason_ros_source_details(
    *,
    season=2026,
    position="RB",
    source_scoring="PPR",
    source_player_count=1,
    source_position_counts=None,
):
    prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
        source_scoring
    ]
    path = f"/nfl/rankings/ros-{prefix}{position.casefold()}.php"
    heading = f"Fantasy Football ROS Rankings ({season})"
    note = (
        f"We are currently displaying {season} Draft Rankings. "
        "Updated ROS Rankings will be available after the first week."
    )
    return EcrSourceDetails(
        ranking_type="draft",
        type_text={
            "STD": "Draft", "HALF": "Draft Half PPR", "PPR": "Draft PPR",
        }[source_scoring],
        source_week=0,
        page_position=position,
        source_player_count=source_player_count,
        source_position_counts=(
            {position: source_player_count}
            if source_position_counts is None
            else source_position_counts
        ),
        expert_selection_policy=FANTASYPROS_LATEST_ECR_POLICY,
        expert_group_id="default",
        expert_group_title="Latest ECR",
        expert_group_description="More accurate experts with recent updates",
        page_protocol="https:",
        page_hostname="www.fantasypros.com",
        page_port="",
        page_path=path,
        canonical_protocol="https:",
        canonical_hostname="www.fantasypros.com",
        canonical_port="",
        canonical_path=path,
        canonical_link_count=1,
        document_title="Rest of Season Rankings | FantasyPros",
        settings_ranking_type="ros",
        settings_position=position,
        settings_page_heading=heading,
        settings_fallback_note=note,
        visible_page_heading=heading,
        visible_page_heading_count=1,
        visible_ranking_period="Rest of Season",
        visible_ranking_period_count=1,
        visible_fallback_note=note,
        visible_fallback_note_count=1,
        horizon_evidence=EcrHorizonEvidence.PRESEASON_REST_OF_SEASON_PAGE,
    )


def with_ecr_rankings(snapshot, rankings):
    """Replace fixture rankings while keeping page source-count proof coherent."""

    rows = tuple(rankings)
    counts = Counter(row.position for row in rows)
    panels = []
    for panel in snapshot.expert_panels:
        source_counts = dict(panel.provenance.source_details.source_position_counts)
        source_counts[panel.position] = counts[panel.position]
        details = replace(
            panel.provenance.source_details,
            source_player_count=sum(source_counts.values()),
            source_position_counts=source_counts,
        )
        panels.append(
            replace(
                panel,
                provenance=replace(panel.provenance, source_details=details),
            )
        )
    return replace(snapshot, rankings=rows, expert_panels=tuple(panels))


__all__ = (
    "ecr_source_details",
    "ecr_source_provenance",
    "preseason_ros_source_details",
    "with_ecr_rankings",
)
