"""Atomic, auditable Excel export for qualified three-team trades."""

from collections.abc import Iterable
from datetime import timezone
from itertools import islice
import os
from pathlib import Path
from uuid import uuid4

from .surrogate_disclosure import SURROGATE_NOTICE
from .three_way_workbook import ThreeWayExportProvenance, ThreeWayWorkbookRow
from .workbook_model import TradeWorkbookContext, WorkbookTeamOutlook
from .xlsx_export import data_readiness_detail_rows, write_team_outlook_sheet


MAX_THREE_WAY_EXPORT_ROWS = 10_000
_TEAM_FIELDS = (
    "Team", "Give", "Receive", "Adds", "Drops", "Power Δ",
    "Playoff Before", "Playoff After", "Playoff Δ",
)
TRADE_HEADERS = (
    "Player Movement",
    *(
        f"Team {number} {field}"
        for number in range(1, 4)
        for field in _TEAM_FIELDS
    ),
    "All 3 Improve",
    "Combined Playoff Δ",
    "Candidate Index",
    "Power Method Evidence",
)


def export_three_way_trade_workbook(
    output_path: str | os.PathLike[str],
    context: TradeWorkbookContext,
    provenance: ThreeWayExportProvenance,
    trade_rows: Iterable[ThreeWayWorkbookRow],
    team_outlook: Iterable[WorkbookTeamOutlook],
) -> Path:
    """Replace one workbook atomically with three-team results and provenance."""

    if not isinstance(context, TradeWorkbookContext):
        raise ValueError("context must be a TradeWorkbookContext")
    if not isinstance(provenance, ThreeWayExportProvenance):
        raise ValueError("provenance must be a ThreeWayExportProvenance")
    trades = _bounded_trade_rows(trade_rows)
    outlook = tuple(team_outlook)
    if any(not isinstance(row, ThreeWayWorkbookRow) for row in trades):
        raise ValueError("trade_rows must contain ThreeWayWorkbookRow values")
    if any(not isinstance(row, WorkbookTeamOutlook) for row in outlook):
        raise ValueError("team_outlook must contain WorkbookTeamOutlook values")
    _validate_export_binding(context, provenance, trades)
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


def require_three_way_exportable_count(value: int) -> None:
    """Fail before result records are materialized when an export is too large."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("qualified trade count must be a non-negative integer")
    if value > MAX_THREE_WAY_EXPORT_ROWS:
        raise ValueError(
            "Three-team Excel export supports at most "
            f"{MAX_THREE_WAY_EXPORT_ROWS:,} qualified trades; tighten the search "
            "filters before exporting"
        )


def _bounded_trade_rows(values):
    try:
        trades = tuple(islice(iter(values), MAX_THREE_WAY_EXPORT_ROWS + 1))
    except TypeError:
        raise ValueError("trade_rows must be an iterable") from None
    require_three_way_exportable_count(len(trades))
    return trades


def _write_workbook(path, context, provenance, trades, outlook):
    try:
        import xlsxwriter
    except ImportError:
        raise RuntimeError("Excel export support is not installed") from None

    options = {"constant_memory": False, "strings_to_formulas": False, "strings_to_urls": False}
    workbook = xlsxwriter.Workbook(path, options)
    try:
        workbook.set_properties(
            {
                "title": f"Three-way trade analysis for {context.primary_team_name}",
                "subject": "Locally calculated three-team fantasy-football trades",
                "author": "Fantasy Trade Evaluator",
                "comments": "Generated from an immutable weekly snapshot.",
            }
        )
        formats = _formats(workbook)
        best = tuple(row for row in trades if row.all_teams_gain)
        _trade_sheet(
            workbook,
            "Best Three-Way",
            best,
            context,
            formats,
            "BestThreeWayTradesTable",
        )
        _trade_sheet(
            workbook,
            "All Qualified",
            trades,
            context,
            formats,
            "AllThreeWayTradesTable",
        )
        write_team_outlook_sheet(
            workbook,
            outlook,
            formats,
            table_name="ThreeWayTeamOutlookTable",
        )
        _details_sheet(
            workbook, context, provenance, len(trades), len(best), formats
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
        "wrapped": workbook.add_format({"valign": "top", "text_wrap": True}),
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
            {"bold": True, "font_color": "#991B1B", "bg_color": "#FEE2E2", "valign": "top", "text_wrap": True}
        ),
    }


def _trade_sheet(workbook, name, rows, context, formats, table_name):
    sheet = workbook.add_worksheet(name)
    sheet.hide_gridlines(2)
    sheet.set_tab_color("#0F766E" if name == "Best Three-Way" else "#246B7B")
    sheet.set_row(0, 30)
    sheet.merge_range(
        0,
        0,
        0,
        len(TRADE_HEADERS) - 1,
        f"{name} — {context.primary_team_name}",
        formats["title"],
    )
    cards = (
        (0, "Trades", len(rows), "card_number"),
        (4, "All three improve", sum(row.all_teams_gain for row in rows), "card_number"),
        (
            8,
            "Best combined odds gain",
            max((row.combined_playoff_delta for row in rows), default=0),
            "card_percent",
        ),
    )
    for column, label, value, format_name in cards:
        sheet.write(2, column, label, formats["card_label"])
        sheet.write(3, column, value, formats[format_name])
    generated = context.generated_at.astimezone(timezone.utc).replace(tzinfo=None)
    sheet.write(2, 13, "Generated (UTC)", formats["card_label"])
    sheet.write_datetime(3, 13, generated, formats["datetime"])
    header_row = 6
    sheet.write_row(header_row, 0, TRADE_HEADERS, formats["header"])
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
        _trade_conditional_formats(
            sheet, header_row + 1, header_row + len(rows), formats
        )
    else:
        sheet.write(
            header_row + 1,
            0,
            "No three-way trades met these filters.",
            formats["muted"],
        )
    sheet.freeze_panes(header_row + 1, 1)
    sheet.set_column(0, 0, 46)
    for base in (1, 10, 19):
        sheet.set_column(base, base, 20)
        sheet.set_column(base + 1, base + 4, 24)
        sheet.set_column(base + 5, base + 8, 16)
    sheet.set_column(28, 31, 20)
    sheet.set_default_row(30)


def _write_trade_row(sheet, row_number, row, formats):
    excel_row = row_number + 1
    sheet.write(
        row_number,
        0,
        "\n".join(transfer.description for transfer in row.transfers),
        formats["wrapped"],
    )
    playoff_delta_columns = []
    for base, impact in zip((1, 10, 19), row.team_impacts):
        sheet.write(row_number, base, impact.team_name, formats["text"])
        for offset, names in enumerate(
            (
                impact.sent_player_names,
                impact.received_player_names,
                impact.added_player_names,
                impact.dropped_player_names,
            ),
            start=1,
        ):
            sheet.write(row_number, base + offset, "; ".join(names), formats["text"])
        sheet.write_number(row_number, base + 5, impact.power_delta, formats["decimal"])
        sheet.write_number(row_number, base + 6, impact.playoff_before, formats["percent"])
        sheet.write_number(row_number, base + 7, impact.playoff_after, formats["percent"])
        delta_column = base + 8
        playoff_delta_columns.append(delta_column)
        formula = (
            f"={_cell(base + 7, excel_row)}-{_cell(base + 6, excel_row)}"
        )
        sheet.write_formula(
            row_number,
            delta_column,
            formula,
            formats["percent"],
            impact.playoff_delta,
        )
    delta_cells = [_cell(column, excel_row) for column in playoff_delta_columns]
    sheet.write_formula(
        row_number,
        28,
        "=AND(" + ",".join(f"{cell}>0" for cell in delta_cells) + ")",
        None,
        row.all_teams_gain,
    )
    sheet.write_formula(
        row_number,
        29,
        "=" + "+".join(delta_cells),
        formats["percent"],
        row.combined_playoff_delta,
    )
    sheet.write_string(row_number, 30, str(row.candidate_index), formats["text"])
    sheet.write(row_number, 31, row.power_methodology_status, formats["text"])


def _trade_conditional_formats(sheet, first, last, formats):
    for column in (6, 15, 24):
        sheet.conditional_format(
            first, column, last, column,
            {
                "type": "3_color_scale",
                "min_color": "#FECACA",
                "mid_color": "#FEF3C7",
                "max_color": "#BBF7D0",
            },
        )
    for column in (9, 18, 27):
        for criteria, format_name in ((">", "positive"), ("<", "negative")):
            sheet.conditional_format(
                first, column, last, column,
                {
                    "type": "cell",
                    "criteria": criteria,
                    "value": 0,
                    "format": formats[format_name],
                },
            )
    sheet.conditional_format(
        first, 29, last, 29,
        {"type": "data_bar", "bar_color": "#2A9D8F"},
    )


def _details_sheet(
    workbook, context, provenance, trade_count, all_gain_count, formats
):
    sheet = workbook.add_worksheet("Run Details")
    sheet.hide_gridlines(2)
    sheet.set_row(0, 30)
    sheet.merge_range("A1:C1", "Calculation Provenance", formats["title"])
    attested = context.power_engine_mode == "holdout_validated"
    three_way_notice = (
        "EXTRAPOLATED — three-way trades are outside the attested two-team "
        "trade-shape scope; playoff projections remain local."
        if attested
        else "SURROGATE_EXTRAPOLATED — three-way trades are outside the "
        "observed two-team surrogate shapes."
    )
    details = (
        ("Trade Format", "three_team"),
        ("Engine Bundle ID", context.bundle_id),
        ("Waiver Pool ID", context.waiver_pool_id),
        ("Request ID", provenance.request_id),
        ("Search Request (Canonical JSON)", provenance.request_json),
        ("Search Run ID", provenance.search_run_id),
        ("Search Run Definition (Canonical JSON)", provenance.search_run_json),
        *(
            (
                f"Participant Team {index}",
                f"{team_name} ({team_id})",
            )
            for index, (team_id, team_name) in enumerate(
                zip(
                    provenance.participant_team_ids,
                    provenance.participant_team_names,
                ),
                start=1,
            )
        ),
        ("Exact Total Candidate Count", str(provenance.total_candidate_count)),
        ("Random Seed", str(provenance.seed)),
        ("Full Trade Constraints", provenance.trade_constraints_display),
        ("Power Search Settings", provenance.power_settings_display),
        (
            "Scarce Free-Agent Allocation",
            provenance.free_agent_allocation_policy
            or "Not applicable — this search did not allow forced roster adjustments.",
        ),
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
        ("Three-Way Power Method", three_way_notice),
        ("Calibration Evidence Status", context.calibration_status),
        ("Methodology Evidence Type", context.methodology_evidence_kind),
        ("Methodology Evidence Record ID", context.methodology_record_id),
        ("Strength Formula ID", context.formula_id),
        ("Source Fit ID", context.formula_source_fit_id),
        ("Publication Quality Gate", context.methodology_quality_gate),
        ("Blind Holdout Trades", context.methodology_holdout_count),
        ("Maximum Blind Score Error", context.holdout_max_absolute_score_error),
        ("Blind Display Match Rate", context.holdout_display_match_rate),
        ("Methodology Fingerprint ID", context.methodology_fingerprint_id),
        ("Formula Action", context.formula_action),
        ("Current Methodology Evidence ID", context.methodology_current_evidence_id),
        (
            "Blind-Validated FantasyPros-Power Scope",
            "NONE for three-way trades — every three-team result is extrapolated.",
        ),
        (
            "Power Accuracy Notice",
            (
                three_way_notice
                if attested
                else f"{three_way_notice} {SURROGATE_NOTICE}"
            ),
        ),
        *data_readiness_detail_rows(context),
        ("Qualified Three-Way Trades", trade_count),
        ("All-Three Playoff Gains", all_gain_count),
    )
    sheet.write_row("A3", ("Setting", "Value"), formats["header"])
    for row, (label, value) in enumerate(details, start=3):
        sheet.write(row, 0, label, formats["section"])
        value_format = None
        if label == "Blind Display Match Rate":
            value_format = formats["percent"]
        elif label in {
            "Three-Way Power Method",
            "Blind-Validated FantasyPros-Power Scope",
            "Power Accuracy Notice",
        } or label.endswith(" Limitation") or (
            label == "Power Engine Mode" and not attested
        ):
            value_format = formats["warning"]
            sheet.set_row(row, 55)
        elif label in {
            "Full Trade Constraints",
            "Power Search Settings",
        } or label.endswith(" Policy"):
            value_format = formats["wrapped"]
            sheet.set_row(row, 45 if label.endswith(" Policy") else 110)
        sheet.write(row, 1, value, value_format)
    source_row = 4 + len(details)
    sheet.write(source_row, 0, "Weekly Sources", formats["section"])
    sheet.write_row(
        source_row + 1,
        0,
        ("Source", "Evidence ID", "Captured UTC"),
        formats["header"],
    )
    for offset, source in enumerate(context.sources, start=source_row + 2):
        sheet.write(offset, 0, source.name)
        sheet.write(offset, 1, source.evidence_id)
        captured = source.captured_at.astimezone(timezone.utc).replace(tzinfo=None)
        sheet.write_datetime(offset, 2, captured, formats["datetime"])
    sheet.set_column(0, 0, 40)
    sheet.set_column(1, 1, 90)
    sheet.set_column(2, 2, 21)


def _validate_export_binding(context, provenance, trades):
    if context.data_readiness.trade_search_status == "not_ready":
        raise ValueError("cannot export a search whose data readiness is not_ready")
    if (
        context.bundle_id != provenance.bundle_id
        or context.waiver_pool_id != provenance.waiver_pool_id
    ):
        raise ValueError("workbook context does not match bundle provenance")
    request = provenance.request_record
    run = provenance.search_run_definition
    run_inputs = run.trade_constraint_record
    if (
        context.primary_team_id != request["primary_team_id"]
        or context.primary_team_name != provenance.participant_team_names[0]
        or context.scenario_count != request["scenario_count"]
        or context.minimum_power_delta
        != request["settings"]["minimum_displayed_power_delta"]
    ):
        raise ValueError("workbook context does not match the search request")
    if (
        context.snapshot_id != run.snapshot_id
        or context.strength_model_id != run.strength_model_id
        or context.scenario_run_id != run_inputs.get("scenario_run_id")
    ):
        raise ValueError("workbook context does not match the search run")
    candidate_indexes = set()
    participant_ids = set(provenance.participant_team_ids)
    for row in trades:
        if (
            row.candidate_index >= provenance.total_candidate_count
            or row.candidate_index in candidate_indexes
            or {impact.team_id for impact in row.team_impacts} != participant_ids
            or any(
                transfer.source_team_id not in participant_ids
                or transfer.destination_team_id not in participant_ids
                for transfer in row.transfers
            )
        ):
            raise ValueError("three-team trade row does not match its search run")
        candidate_indexes.add(row.candidate_index)


def _cell(column: int, row: int) -> str:
    letters = ""
    value = column + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


__all__ = (
    "MAX_THREE_WAY_EXPORT_ROWS",
    "export_three_way_trade_workbook",
    "require_three_way_exportable_count",
)
