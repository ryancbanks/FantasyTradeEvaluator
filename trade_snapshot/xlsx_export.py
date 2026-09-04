"""Atomic, formula-auditable Excel export for local trade-search results."""

from collections.abc import Iterable
from datetime import timezone
import os
from pathlib import Path
from uuid import uuid4

from ._data_readiness_policy import (
    _BOUNDED_WAIVER_POOL_LIMITATION,
    _HOST_TRADE_LEGALITY_LIMITATION,
)
from .workbook_model import (
    TradeWorkbookContext,
    TwoTeamExportProvenance,
    WorkbookTeamOutlook,
    WorkbookTradeRow,
)
from .surrogate_disclosure import SURROGATE_NOTICE


MAX_EXCEL_DATA_ROWS = 1_000_000
TRADE_HEADERS = (
    "Other Team",
    "You Give",
    "You Receive",
    "Your Adds",
    "Your Drops",
    "Their Adds",
    "Their Drops",
    "Give #",
    "Receive #",
    "Total Players",
    "Your Power Δ",
    "Their Power Δ",
    "Your Playoff Before",
    "Your Playoff After",
    "Your Playoff Δ",
    "Their Playoff Before",
    "Their Playoff After",
    "Their Playoff Δ",
    "Both Improve",
    "Combined Playoff Δ",
    "Other Team ID",
    "Candidate Index",
    "Power Method Evidence",
    "Search Run ID",
)


def export_trade_workbook(
    output_path: str | os.PathLike[str],
    context: TradeWorkbookContext,
    provenance: TwoTeamExportProvenance,
    trade_rows: Iterable[WorkbookTradeRow],
    team_outlook: Iterable[WorkbookTeamOutlook],
) -> Path:
    """Write one replacement workbook; interrupted exports never damage the target."""

    if not isinstance(context, TradeWorkbookContext):
        raise ValueError("context must be a TradeWorkbookContext")
    if not isinstance(provenance, TwoTeamExportProvenance):
        raise ValueError("provenance must be a TwoTeamExportProvenance")
    trades = tuple(trade_rows)
    outlook = tuple(team_outlook)
    if any(not isinstance(row, WorkbookTradeRow) for row in trades):
        raise ValueError("trade_rows must contain WorkbookTradeRow values")
    if any(not isinstance(row, WorkbookTeamOutlook) for row in outlook):
        raise ValueError("team_outlook must contain WorkbookTeamOutlook values")
    _validate_export_binding(context, provenance, trades)
    if len(trades) > MAX_EXCEL_DATA_ROWS:
        raise ValueError(
            f"Excel export supports at most {MAX_EXCEL_DATA_ROWS:,} qualified trades"
        )
    target = Path(output_path)
    if target.suffix.casefold() != ".xlsx":
        raise ValueError("output_path must end in .xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.xlsx")
    try:
        _write_workbook(temporary, context, provenance, trades, outlook)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def _write_workbook(path, context, provenance, trades, outlook):
    try:
        import xlsxwriter
    except ImportError:
        raise RuntimeError("Excel export support is not installed") from None

    workbook = xlsxwriter.Workbook(
        path,
        {
            "constant_memory": False,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    try:
        workbook.set_properties(
            {
                "title": f"Trade analysis for {context.primary_team_name}",
                "subject": "Locally calculated fantasy-football trade results",
                "author": "Fantasy Trade Evaluator",
                "comments": "Generated from an immutable weekly snapshot.",
            }
        )
        formats = _formats(workbook)
        mutual = tuple(row for row in trades if row.is_mutual_gain)
        _trade_sheet(
            workbook,
            "Best Trades",
            tuple(sorted(mutual, key=lambda row: -row.combined_playoff_delta)),
            context,
            formats,
            "BestTradesTable",
        )
        _trade_sheet(
            workbook,
            "All Qualified",
            trades,
            context,
            formats,
            "QualifiedTradesTable",
        )
        write_team_outlook_sheet(
            workbook,
            outlook,
            formats,
            table_name="TeamOutlookTable",
        )
        _details_sheet(
            workbook,
            context,
            provenance,
            len(trades),
            len(mutual),
            formats,
        )
    finally:
        workbook.close()


def _formats(workbook):
    return {
        "title": workbook.add_format(
            {"bold": True, "font_size": 18, "font_color": "#FFFFFF", "bg_color": "#153B50", "align": "left", "valign": "vcenter"}
        ),
        "card_label": workbook.add_format(
            {"bold": True, "font_color": "#52606D", "bottom": 1, "bottom_color": "#CBD5E1"}
        ),
        "card_number": workbook.add_format(
            {"bold": True, "font_size": 14, "font_color": "#153B50", "num_format": "#,##0"}
        ),
        "card_percent": workbook.add_format(
            {"bold": True, "font_size": 14, "font_color": "#0F766E", "num_format": "0.0%"}
        ),
        "header": workbook.add_format(
            {"bold": True, "font_color": "#FFFFFF", "bg_color": "#246B7B", "align": "center", "valign": "vcenter", "text_wrap": True}
        ),
        "text": workbook.add_format({"valign": "top"}),
        "wrapped_text": workbook.add_format({"valign": "top", "text_wrap": True}),
        "integer": workbook.add_format({"num_format": "#,##0"}),
        "decimal": workbook.add_format({"num_format": "0.0"}),
        "percent": workbook.add_format({"num_format": "0.0%"}),
        "datetime": workbook.add_format({"num_format": "yyyy-mm-dd hh:mm"}),
        "muted": workbook.add_format({"font_color": "#64748B"}),
        "section": workbook.add_format(
            {"bold": True, "font_color": "#153B50", "bg_color": "#DDEBF1", "bottom": 1, "bottom_color": "#9FB8C5", "valign": "top"}
        ),
        "positive": workbook.add_format(
            {"font_color": "#166534", "bg_color": "#DCFCE7"}
        ),
        "negative": workbook.add_format(
            {"font_color": "#991B1B", "bg_color": "#FEE2E2"}
        ),
        "warning": workbook.add_format(
            {
                "bold": True,
                "font_color": "#991B1B",
                "bg_color": "#FEE2E2",
                "valign": "top",
                "text_wrap": True,
            }
        ),
    }


def _trade_sheet(workbook, name, rows, context, formats, table_name):
    sheet = workbook.add_worksheet(name)
    sheet.hide_gridlines(2)
    sheet.set_tab_color("#0F766E" if name == "Best Trades" else "#246B7B")
    sheet.set_row(0, 30)
    sheet.merge_range(0, 0, 0, len(TRADE_HEADERS) - 1, f"{name} — {context.primary_team_name}", formats["title"])
    best_gain = max((row.combined_playoff_delta for row in rows), default=0)
    cards = (
        (0, "Trades", len(rows), "card_number"),
        (3, "Mutual gains", sum(row.is_mutual_gain for row in rows), "card_number"),
        (6, "Best combined odds gain", best_gain, "card_percent"),
    )
    for column, label, value, format_name in cards:
        sheet.write(2, column, label, formats["card_label"])
        sheet.write(3, column, value, formats[format_name])
    generated = context.generated_at.astimezone(timezone.utc).replace(tzinfo=None)
    sheet.write(2, 9, "Generated (UTC)", formats["card_label"])
    sheet.write_datetime(3, 9, generated, formats["datetime"])
    header_row = 6
    for column, header in enumerate(TRADE_HEADERS):
        sheet.write(header_row, column, header, formats["header"])
    for offset, row in enumerate(rows, start=1):
        _write_trade_row(sheet, header_row + offset, row, formats)
    if rows:
        sheet.add_table(
            header_row,
            0,
            header_row + len(rows),
            len(TRADE_HEADERS) - 1,
            {
                "name": table_name,
                "style": "Table Style Medium 2",
                "columns": [{"header": header} for header in TRADE_HEADERS],
            },
        )
        first, last = header_row + 1, header_row + len(rows)
        _trade_conditional_formats(sheet, first, last, formats)
    else:
        sheet.write(header_row + 1, 0, "No trades met these filters.", formats["muted"])
    sheet.freeze_panes(header_row + 1, 1)
    _trade_widths(sheet)


def _write_trade_row(sheet, row_number, row, formats):
    excel_row = row_number + 1
    sheet.write(row_number, 0, row.counterparty_team_name, formats["text"])
    sheet.write(row_number, 1, "; ".join(row.outgoing_player_names), formats["text"])
    sheet.write(row_number, 2, "; ".join(row.incoming_player_names), formats["text"])
    sheet.write(row_number, 3, "; ".join(row.primary_added_player_names), formats["text"])
    sheet.write(row_number, 4, "; ".join(row.primary_dropped_player_names), formats["text"])
    sheet.write(row_number, 5, "; ".join(row.counterparty_added_player_names), formats["text"])
    sheet.write(row_number, 6, "; ".join(row.counterparty_dropped_player_names), formats["text"])
    sheet.write_number(row_number, 7, len(row.outgoing_player_ids), formats["integer"])
    sheet.write_number(row_number, 8, len(row.incoming_player_ids), formats["integer"])
    sheet.write_formula(row_number, 9, f"=H{excel_row}+I{excel_row}", formats["integer"], len(row.outgoing_player_ids) + len(row.incoming_player_ids))
    sheet.write_number(row_number, 10, row.primary_power_delta, formats["decimal"])
    sheet.write_number(row_number, 11, row.counterparty_power_delta, formats["decimal"])
    sheet.write_number(row_number, 12, row.primary_playoff_before, formats["percent"])
    sheet.write_number(row_number, 13, row.primary_playoff_after, formats["percent"])
    sheet.write_formula(row_number, 14, f"=N{excel_row}-M{excel_row}", formats["percent"], row.primary_playoff_delta)
    sheet.write_number(row_number, 15, row.counterparty_playoff_before, formats["percent"])
    sheet.write_number(row_number, 16, row.counterparty_playoff_after, formats["percent"])
    sheet.write_formula(row_number, 17, f"=Q{excel_row}-P{excel_row}", formats["percent"], row.counterparty_playoff_delta)
    sheet.write_formula(row_number, 18, f"=AND(O{excel_row}>0,R{excel_row}>0)", None, row.is_mutual_gain)
    sheet.write_formula(row_number, 19, f"=O{excel_row}+R{excel_row}", formats["percent"], row.combined_playoff_delta)
    sheet.write(row_number, 20, row.counterparty_team_id)
    sheet.write_number(row_number, 21, row.candidate_index, formats["integer"])
    sheet.write(row_number, 22, row.power_methodology_status, formats["text"])
    sheet.write(row_number, 23, row.search_run_id, formats["text"])


def _trade_conditional_formats(sheet, first, last, formats):
    for column in (10, 11):
        sheet.conditional_format(first, column, last, column, {"type": "3_color_scale", "min_color": "#FECACA", "mid_color": "#FEF3C7", "max_color": "#BBF7D0"})
    for column in (14, 17):
        sheet.conditional_format(first, column, last, column, {"type": "cell", "criteria": ">", "value": 0, "format": formats["positive"]})
        sheet.conditional_format(first, column, last, column, {"type": "cell", "criteria": "<", "value": 0, "format": formats["negative"]})
    sheet.conditional_format(first, 19, last, 19, {"type": "data_bar", "bar_color": "#2A9D8F"})


def _trade_widths(sheet):
    widths = (20, 30, 30, 24, 24, 24, 24, 9, 10, 11, 13, 13, 17, 17, 15, 18, 18, 16, 13, 19, 16, 13, 22, 26)
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
    sheet.set_column(20, 21, None, None, {"hidden": True})
    sheet.set_column(23, 23, None, None, {"hidden": True})
    sheet.set_default_row(18)


def write_team_outlook_sheet(workbook, rows, formats, *, table_name):
    """Write the shared 14-field team-outlook contract."""

    sheet = workbook.add_worksheet("Team Outlook")
    sheet.hide_gridlines(2)
    sheet.set_row(0, 30)
    headers = (
        "Team", "Current Rank", "Current W", "Current L", "Current T",
        "Expected W", "Expected L", "Expected T", "Expected PF", "Expected PA",
        "Mean Rank", "Playoff Chance", "Rank Probabilities", "Seed Probabilities",
    )
    sheet.merge_range(0, 0, 0, len(headers) - 1, "Projected Standings and Playoff Outlook", formats["title"])
    for column, header in enumerate(headers):
        sheet.write(2, column, header, formats["header"])
    for index, row in enumerate(rows, start=3):
        values = (
            row.team_name,
            row.current_rank,
            row.current_wins,
            row.current_losses,
            row.current_ties,
            row.expected_final_wins,
            row.expected_final_losses,
            row.expected_final_ties,
            row.expected_final_points_for,
            row.expected_final_points_against,
            row.mean_rank,
            row.playoff_probability,
            _distribution_text(row.rank_distribution),
            _distribution_text(row.seed_distribution),
        )
        for column, value in enumerate(values):
            fmt = (
                formats["text"]
                if column in {0, 12, 13}
                else formats["percent"]
                if column == 11
                else formats["decimal"]
                if 5 <= column <= 10
                else formats["integer"]
            )
            if value is None:
                sheet.write_blank(index, column, None, fmt)
            else:
                sheet.write(index, column, value, fmt)
    if rows:
        sheet.add_table(
            2,
            0,
            2 + len(rows),
            len(headers) - 1,
            {
                "name": table_name,
                "style": "Table Style Medium 2",
                "columns": [{"header": value} for value in headers],
            },
        )
        sheet.conditional_format(
            3,
            11,
            2 + len(rows),
            11,
            {"type": "data_bar", "bar_color": "#2A9D8F"},
        )
    sheet.freeze_panes(3, 1)
    sheet.set_column(0, 0, 24)
    sheet.set_column(1, 11, 14)
    sheet.set_column(12, 13, 34)


def _distribution_text(values):
    return "; ".join(
        f"{index}: {value:.1%}" for index, value in enumerate(values, 1)
    )


def data_readiness_detail_rows(context):
    """Return the immutable data coverage and limitation rows for an export."""

    readiness = context.data_readiness
    rows = (
        ("Power-Score Readiness", readiness.power_score_status),
        ("Trade-Search Readiness", readiness.trade_search_status),
        ("Expected-Standings Readiness", readiness.expected_standings_status),
        ("Playoff Model Readiness", readiness.playoff_model_status),
        ("Projection Provider Cells", readiness.provider_cell_count),
        ("Direct Provider Projection Cells", readiness.direct_provider_cells),
        (
            "ROS-Derived Provider Projection Cells",
            readiness.ros_derived_provider_cells,
        ),
        (
            "Schedule-Derived Availability Cells",
            readiness.schedule_derived_availability_cells,
        ),
        (
            "Unavailable Provider Projection Cells",
            readiness.unavailable_provider_cells,
        ),
        (
            "Unattributed Provider Projection Cells",
            readiness.unattributed_provider_cells,
        ),
        ("First-Week Scheduled NFL Games", readiness.first_week_scheduled_games),
        (
            "First-Week Games Missing Kickoff Time",
            readiness.first_week_games_missing_kickoff,
        ),
        (
            "Source Capture Timestamps",
            readiness.source_capture_timestamp_count,
        ),
        (
            "Earliest Source Capture (UTC)",
            _utc_timestamp(readiness.earliest_source_capture_at),
        ),
        (
            "Latest Source Capture (UTC)",
            _utc_timestamp(readiness.latest_source_capture_at),
        ),
        (
            "Source Capture Window (Seconds)",
            int(
                (
                    readiness.latest_source_capture_at
                    - readiness.earliest_source_capture_at
                ).total_seconds()
            ),
        ),
        (
            "FantasyPros Comparison Team Coverage",
            readiness.fantasypros_comparison_team_count,
        ),
        (
            "Scenario Player-Score Floor",
            (
                readiness.scenario_player_score_floor
                if readiness.scenario_player_score_floor is not None
                else "UNBOUNDED"
            ),
        ),
        (
            "FantasyPros Comparison Policy",
            readiness.fantasypros_comparison_policy,
        ),
        ("Projection Source Artifacts", readiness.projection_source_count),
        (
            "Projection Source Attempts Captured",
            readiness.captured_projection_source_attempts,
        ),
        (
            "Projection Source Attempts Not Published",
            readiness.not_published_projection_source_attempts,
        ),
        (
            "Projection Source Attempts Unavailable",
            readiness.unavailable_projection_source_attempts,
        ),
        (
            "Provider-Total Projection Sources",
            readiness.provider_total_projection_sources,
        ),
        (
            "Locally Recomputed Projection Sources",
            readiness.locally_recomputed_projection_sources,
        ),
        (
            "Base-Format-Only Projection Sources",
            readiness.base_format_only_projection_sources,
        ),
        (
            "Exact-Host-Rules Projection Sources",
            readiness.exact_host_rules_projection_sources,
        ),
        (
            "Projection Source Scoring Formats",
            ", ".join(readiness.projection_source_scoring_formats),
        ),
        (
            "Provider Status Observations",
            readiness.provider_status_observation_count,
        ),
        (
            "Provider Status Disagreement Scopes",
            readiness.provider_status_disagreement_scope_count,
        ),
        (
            "Latest Provider Status Observation (UTC)",
            (
                "NONE RETAINED"
                if readiness.latest_provider_status_observed_at is None
                else _utc_timestamp(readiness.latest_provider_status_observed_at)
            ),
        ),
        ("Player-Availability Limitation", readiness.availability_limitation),
        ("Outcome-Correlation Limitation", readiness.correlation_limitation),
        (
            "Marginal-Uncertainty Limitation",
            readiness.marginal_uncertainty_limitation,
        ),
        (
            "Championship-Proxy Limitation",
            readiness.championship_proxy_limitation,
        ),
        (
            "Host-Settlement-Policy Limitation",
            readiness.host_settlement_policy_limitation,
        ),
        ("Bounded-Waiver-Pool Limitation", _BOUNDED_WAIVER_POOL_LIMITATION),
        ("Host-Trade-Legality Limitation", _HOST_TRADE_LEGALITY_LIMITATION),
    )
    if readiness.custom_scoring_limitation:
        rows = (
            *rows,
            ("Custom-Scoring Limitation", readiness.custom_scoring_limitation),
        )
    if readiness.as_of_time_limitation:
        rows = (*rows, ("As-of-Time Limitation", readiness.as_of_time_limitation))
    if readiness.ros_allocation_limitation:
        rows = (
            *rows,
            ("ROS Weekly-Allocation Limitation", readiness.ros_allocation_limitation),
        )
    rows = (
        *rows,
        *(
            (
                f"Projection Attempts ({provider})",
                (
                    f"captured={captured}; not_published={not_published}; "
                    f"unavailable={unavailable}"
                ),
            )
            for provider, captured, not_published, unavailable in (
                readiness.projection_source_provider_attempts
            )
        ),
    )
    return rows


def _details_sheet(
    workbook, context, provenance, trade_count, mutual_count, formats
):
    sheet = workbook.add_worksheet("Run Details")
    sheet.hide_gridlines(2)
    sheet.set_row(0, 30)
    sheet.merge_range("A1:D1", "Calculation Provenance", formats["title"])
    attested = context.power_engine_mode == "holdout_validated"
    details = (
        ("Engine Bundle ID", provenance.bundle_id),
        ("Waiver Pool ID", provenance.waiver_pool_id),
        ("Search Request ID", provenance.request_id),
        ("Search Request (Canonical JSON)", provenance.request_json),
        ("Trade Constraints (Canonical JSON)", provenance.trade_constraints_json),
        ("Power Settings (Canonical JSON)", provenance.search_settings_json),
        ("Require No Drops", "YES" if provenance.require_no_drops else "NO"),
        ("Scenario Seed", provenance.scenario_seed),
        ("Requested Counterparty Scope", provenance.requested_counterparty_display),
        (
            "Resolved Counterparty Team IDs",
            ", ".join(provenance.resolved_counterparty_team_ids),
        ),
        (
            "Roster Adjustment IDs",
            ", ".join(provenance.roster_adjustment_ids),
        ),
        ("Pair Search Runs", len(provenance.search_runs)),
        ("Total Search Candidates", provenance.total_candidate_count),
        ("Snapshot ID", context.snapshot_id),
        ("Scoring Profile ID", context.scoring_profile_id),
        ("NFL Schedule ID", context.nfl_schedule_id),
        ("Ensemble Configuration ID", context.ensemble_config_id),
        ("Strength Model ID", context.strength_model_id),
        ("Scenario Run ID", context.scenario_run_id),
        ("Primary Team", context.primary_team_name),
        ("Primary Team ID", context.primary_team_id),
        ("Minimum Power Delta", context.minimum_power_delta),
        ("Simulation Scenarios", context.scenario_count),
        (
            "Power Engine Mode",
            (
                "BLIND-HOLDOUT VALIDATED"
                if attested
                else "SURROGATE / APPROXIMATE"
            ),
        ),
        ("Calibration Evidence Status", context.calibration_status),
        ("Methodology Evidence Type", context.methodology_evidence_kind),
        ("Methodology Evidence Record ID", context.methodology_record_id),
        ("Strength Formula ID", context.formula_id),
        ("Source Fit ID", context.formula_source_fit_id),
        ("Publication Quality Gate", context.methodology_quality_gate),
        ("Blind Holdout Trades", context.methodology_holdout_count),
        (
            "Maximum Blind Score Error",
            context.holdout_max_absolute_score_error,
        ),
        ("Blind Display Match Rate", context.holdout_display_match_rate),
        ("Methodology Fingerprint ID", context.methodology_fingerprint_id),
        ("Formula Action", context.formula_action),
        (
            "Current Methodology Evidence ID",
            context.methodology_current_evidence_id,
        ),
        (
            "Blind-Validated FantasyPros-Power Scope",
            (
                "Representative balanced, no-add/drop holdouts for package sizes "
                + ", ".join(
                    str(value)
                    for value in (
                        context.holdout_validated_balanced_package_sizes
                    )
                )
                if attested
                else "NONE — this engine is a SURROGATE approximation"
            ),
        ),
        (
            "Power Accuracy Notice",
            (
                "The listed shapes passed representative blind holdouts; this is "
                "not exhaustive proof for every player combination. Other shapes "
                "are labeled extrapolated; playoff projections remain local."
                if attested
                else SURROGATE_NOTICE
            ),
        ),
        *data_readiness_detail_rows(context),
        ("Qualified Trades", trade_count),
        ("Mutual Playoff Gains", mutual_count),
    )
    sheet.write_row("A3", ("Setting", "Value"), formats["header"])
    for row, values in enumerate(details, start=3):
        label, value = values
        sheet.write(row, 0, label, formats["section"])
        value_format = None
        if label == "Blind Display Match Rate":
            value_format = formats["percent"]
        elif label == "Power Accuracy Notice":
            value_format = (
                formats["wrapped_text"] if attested else formats["warning"]
            )
            sheet.set_row(row, 60)
        elif label.endswith(" Limitation"):
            value_format = formats["warning"]
            sheet.set_row(row, 60)
        elif label.endswith(" Policy") or label.endswith("(Canonical JSON)"):
            value_format = formats["wrapped_text"]
            sheet.set_row(row, 45)
        elif label == "Power Engine Mode" and not attested:
            value_format = formats["warning"]
        sheet.write(row, 1, value, value_format)
    run_row = 4 + len(details)
    sheet.write(run_row, 0, "Pair Search Definitions", formats["section"])
    sheet.write_row(
        run_row + 1,
        0,
        ("Counterparty Team ID", "Search Run ID", "Candidates", "Definition JSON"),
        formats["header"],
    )
    for offset, run in enumerate(provenance.search_run_rows, start=run_row + 2):
        sheet.write_row(offset, 0, run)
    source_row = run_row + len(provenance.search_runs) + 3
    sheet.write(source_row, 0, "Weekly Sources", formats["section"])
    sheet.write_row(source_row + 1, 0, ("Source", "Evidence ID", "Captured UTC"), formats["header"])
    for offset, source in enumerate(context.sources, start=source_row + 2):
        sheet.write(offset, 0, source.name)
        sheet.write(offset, 1, source.evidence_id)
        captured = source.captured_at.astimezone(timezone.utc).replace(tzinfo=None)
        sheet.write_datetime(offset, 2, captured, formats["datetime"])
    sheet.set_column(0, 0, 40)
    sheet.set_column(1, 1, 82)
    sheet.set_column(2, 2, 21)
    sheet.set_column(3, 3, 90)


def _validate_export_binding(context, provenance, trades):
    request = provenance.request_record
    if context.data_readiness.trade_search_status == "not_ready":
        raise ValueError("cannot export a search whose data readiness is not_ready")
    if (
        provenance.bundle_id != context.bundle_id
        or provenance.waiver_pool_id != context.waiver_pool_id
    ):
        raise ValueError("workbook context does not match bundle provenance")
    if (
        request["primary_team_id"] != context.primary_team_id
        or request["scenario_count"] != context.scenario_count
        or request["settings"]["minimum_displayed_power_delta"]
        != context.minimum_power_delta
    ):
        raise ValueError("workbook context does not match the search request")
    by_run = {row.run_id: row for row in provenance.search_runs}
    for run in provenance.search_runs:
        definition = run.trade_constraint_record
        if (
            run.snapshot_id != context.snapshot_id
            or run.strength_model_id != context.strength_model_id
            or definition["scenario_run_id"] != context.scenario_run_id
        ):
            raise ValueError("workbook context does not match a pair search run")
    row_keys = set()
    for row in trades:
        run = by_run.get(row.search_run_id)
        key = (row.search_run_id, row.candidate_index)
        if (
            run is None
            or run.counterparty_team_id != row.counterparty_team_id
            or row.candidate_index >= run.total_candidate_count
            or key in row_keys
        ):
            raise ValueError("trade row does not match its pair search run")
        row_keys.add(key)


def _utc_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
