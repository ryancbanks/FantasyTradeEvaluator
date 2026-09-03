"""Atomic, formula-auditable Excel export for local trade-search results."""

from collections.abc import Iterable
from datetime import timezone
import os
from pathlib import Path
from uuid import uuid4

from .workbook_model import (
    TradeWorkbookContext,
    WorkbookTeamOutlook,
    WorkbookTradeRow,
    WorkbookTradeRows,
)
from .surrogate_disclosure import SURROGATE_NOTICE


MAX_EXCEL_DATA_ROWS = 1_000_000
# XlsxWriter's ordinary mode retains every worksheet cell until close. Switch
# larger exports to its row-streaming mode before that becomes the dominant
# memory cost; smaller workbooks retain native Excel tables.
_MAX_IN_MEMORY_TRADE_ROWS = 10_000
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
)


def export_trade_workbook(
    output_path: str | os.PathLike[str],
    context: TradeWorkbookContext,
    trade_rows: Iterable[WorkbookTradeRow],
    team_outlook: Iterable[WorkbookTeamOutlook],
) -> Path:
    """Write one replacement workbook; interrupted exports never damage the target."""

    if not isinstance(context, TradeWorkbookContext):
        raise ValueError("context must be a TradeWorkbookContext")
    if isinstance(trade_rows, WorkbookTradeRows):
        trades = trade_rows
    else:
        trades = tuple(trade_rows)
    outlook = tuple(team_outlook)
    if not isinstance(trades, WorkbookTradeRows) and any(
        not isinstance(row, WorkbookTradeRow) for row in trades
    ):
        raise ValueError("trade_rows must contain WorkbookTradeRow values")
    if any(not isinstance(row, WorkbookTeamOutlook) for row in outlook):
        raise ValueError("team_outlook must contain WorkbookTeamOutlook values")
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
        _write_workbook(temporary, context, trades, outlook)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def _write_workbook(path, context, trades, outlook):
    try:
        import xlsxwriter
    except ImportError:
        raise RuntimeError("Excel export support is not installed") from None

    constant_memory = len(trades) > _MAX_IN_MEMORY_TRADE_ROWS
    workbook = xlsxwriter.Workbook(
        path,
        {
            "constant_memory": constant_memory,
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
        if isinstance(trades, WorkbookTradeRows):
            mutual_count = trades.mutual_count
            mutual = trades.iter_mutual()
        else:
            mutual = tuple(row for row in trades if row.is_mutual_gain)
            mutual_count = len(mutual)
            mutual = tuple(
                sorted(mutual, key=lambda row: -row.combined_playoff_delta)
            )
        _trade_sheet(
            workbook,
            "Best Trades",
            mutual,
            mutual_count,
            mutual_count,
            context,
            formats,
            "BestTradesTable",
            use_table=not constant_memory,
        )
        _trade_sheet(
            workbook,
            "All Qualified",
            trades,
            len(trades),
            mutual_count,
            context,
            formats,
            "QualifiedTradesTable",
            use_table=not constant_memory,
        )
        _outlook_sheet(workbook, outlook, formats, use_table=not constant_memory)
        _details_sheet(workbook, context, len(trades), mutual_count, formats)
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


def _trade_sheet(
    workbook,
    name,
    rows,
    row_count,
    mutual_count,
    context,
    formats,
    table_name,
    *,
    use_table,
):
    sheet = workbook.add_worksheet(name)
    sheet.hide_gridlines(2)
    sheet.set_tab_color("#0F766E" if name == "Best Trades" else "#246B7B")
    sheet.set_row(0, 30)
    sheet.merge_range(0, 0, 0, len(TRADE_HEADERS) - 1, f"{name} — {context.primary_team_name}", formats["title"])
    sheet.write(2, 0, "Trades", formats["card_label"])
    sheet.write(2, 3, "Mutual gains", formats["card_label"])
    sheet.write(2, 6, "Best combined odds gain", formats["card_label"])
    sheet.write(2, 9, "Generated (UTC)", formats["card_label"])
    sheet.write(3, 0, row_count, formats["card_number"])
    sheet.write(3, 3, mutual_count, formats["card_number"])
    header_row = 6
    if row_count:
        first_excel_row = header_row + 2
        last_excel_row = header_row + row_count + 1
        sheet.write_formula(
            3,
            6,
            f"=MAX(T{first_excel_row}:T{last_excel_row})",
            formats["card_percent"],
            0,
        )
    else:
        sheet.write(3, 6, 0, formats["card_percent"])
    generated = context.generated_at.astimezone(timezone.utc).replace(tzinfo=None)
    sheet.write_datetime(3, 9, generated, formats["datetime"])
    for column, header in enumerate(TRADE_HEADERS):
        sheet.write(header_row, column, header, formats["header"])
    written_count = 0
    for offset, row in enumerate(rows, start=1):
        if not isinstance(row, WorkbookTradeRow):
            raise ValueError("trade_rows must contain WorkbookTradeRow values")
        _write_trade_row(sheet, header_row + offset, row, formats)
        written_count = offset
    if written_count != row_count:
        raise ValueError("trade row count changed while the workbook was written")
    if row_count:
        if use_table:
            sheet.add_table(
                header_row,
                0,
                header_row + row_count,
                len(TRADE_HEADERS) - 1,
                {
                    "name": table_name,
                    "style": "Table Style Medium 2",
                    "columns": [{"header": header} for header in TRADE_HEADERS],
                },
            )
        else:
            sheet.autofilter(
                header_row,
                0,
                header_row + row_count,
                len(TRADE_HEADERS) - 1,
            )
        first, last = header_row + 1, header_row + row_count
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


def _trade_conditional_formats(sheet, first, last, formats):
    for column in (10, 11):
        sheet.conditional_format(first, column, last, column, {"type": "3_color_scale", "min_color": "#FECACA", "mid_color": "#FEF3C7", "max_color": "#BBF7D0"})
    for column in (14, 17):
        sheet.conditional_format(first, column, last, column, {"type": "cell", "criteria": ">", "value": 0, "format": formats["positive"]})
        sheet.conditional_format(first, column, last, column, {"type": "cell", "criteria": "<", "value": 0, "format": formats["negative"]})
    sheet.conditional_format(first, 19, last, 19, {"type": "data_bar", "bar_color": "#2A9D8F"})


def _trade_widths(sheet):
    widths = (20, 30, 30, 24, 24, 24, 24, 9, 10, 11, 13, 13, 17, 17, 15, 18, 18, 16, 13, 19, 16, 13, 22)
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
    sheet.set_column(20, 21, None, None, {"hidden": True})
    sheet.set_default_row(18)


def _outlook_sheet(workbook, rows, formats, *, use_table):
    sheet = workbook.add_worksheet("Team Outlook")
    sheet.hide_gridlines(2)
    sheet.set_row(0, 30)
    headers = ("Team", "Current W", "Current L", "Current T", "Expected W", "Expected L", "Expected T", "Mean Rank", "Playoff Chance")
    sheet.merge_range(0, 0, 0, len(headers) - 1, "Projected Standings and Playoff Outlook", formats["title"])
    for column, header in enumerate(headers):
        sheet.write(2, column, header, formats["header"])
    for index, row in enumerate(rows, start=3):
        values = (row.team_name, row.current_wins, row.current_losses, row.current_ties, row.expected_final_wins, row.expected_final_losses, row.expected_final_ties, row.mean_rank, row.playoff_probability)
        for column, value in enumerate(values):
            fmt = formats["text"] if column == 0 else formats["percent"] if column == 8 else formats["decimal"] if column >= 4 else formats["integer"]
            sheet.write(index, column, value, fmt)
    if rows:
        if use_table:
            sheet.add_table(2, 0, 2 + len(rows), len(headers) - 1, {"name": "TeamOutlookTable", "style": "Table Style Medium 2", "columns": [{"header": value} for value in headers]})
        else:
            sheet.autofilter(2, 0, 2 + len(rows), len(headers) - 1)
        sheet.conditional_format(3, 8, 2 + len(rows), 8, {"type": "data_bar", "bar_color": "#2A9D8F"})
    sheet.freeze_panes(3, 1)
    sheet.set_column(0, 0, 24)
    sheet.set_column(1, 8, 14)


def _details_sheet(workbook, context, trade_count, mutual_count, formats):
    sheet = workbook.add_worksheet("Run Details")
    sheet.hide_gridlines(2)
    sheet.set_row(0, 30)
    sheet.merge_range("A1:C1", "Calculation Provenance", formats["title"])
    exact = context.power_engine_mode == "exact"
    details = (
        ("Snapshot ID", context.snapshot_id),
        ("Strength Model ID", context.strength_model_id),
        ("Scenario Run ID", context.scenario_run_id),
        ("Primary Team", context.primary_team_name),
        ("Primary Team ID", context.primary_team_id),
        ("Minimum Power Delta", context.minimum_power_delta),
        ("Simulation Scenarios", context.scenario_count),
        (
            "Power Engine Mode",
            "EXACT / ATTESTED" if exact else "SURROGATE / APPROXIMATE",
        ),
        ("Calibration Status", context.calibration_status),
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
            "Exact FantasyPros-Power Scope",
            (
                "balanced, no adds/drops, package sizes "
                + ", ".join(
                    str(value) for value in context.exact_balanced_package_sizes
                )
                if exact
                else "NONE — this engine is a SURROGATE approximation"
            ),
        ),
        (
            "Power Accuracy Notice",
            (
                "Outside the attested scope, FantasyPros-style power is labeled "
                "extrapolated; playoff projections remain local."
                if exact
                else SURROGATE_NOTICE
            ),
        ),
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
            value_format = formats["wrapped_text"] if exact else formats["warning"]
            sheet.set_row(row, 60)
        elif label == "Power Engine Mode" and not exact:
            value_format = formats["warning"]
        sheet.write(row, 1, value, value_format)
    source_row = 4 + len(details)
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
