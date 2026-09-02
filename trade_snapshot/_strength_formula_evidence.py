"""Normalize the blind-trade scope carried by a portable strength formula."""

from ._calibration_results import FittedStrengthCalibration, MINIMUM_EXACT_TRADE_COUNT
from .strength import CalibrationStatus
from .strength_calibration import CalibrationMetadata


def normalize_formula_evidence(
    calibration: CalibrationMetadata,
    trade_ids: object,
    balanced_package_sizes: object,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    ids = _heldout_ids(trade_ids)
    sizes = _balanced_package_sizes(balanced_package_sizes)
    if len(ids) != calibration.held_out_trade_count:
        raise ValueError("held_out_trade_ids must exactly cover calibration holdouts")
    if sizes and not ids:
        raise ValueError(
            "held-out balanced package sizes require held-out trade evidence"
        )
    if calibration.status is CalibrationStatus.EXACT and (
        len(ids) < MINIMUM_EXACT_TRADE_COUNT
        or not {2, 3, 4}.issubset(sizes)
    ):
        raise ValueError(
            "exact strength formulas require at least 100 blind trade IDs "
            "covering balanced 2-for-2, 3-for-3, and 4-for-4 packages"
        )
    return ids, sizes


def fitted_formula_evidence(
    fitted: FittedStrengthCalibration,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    ids = tuple(row.trade_id for row in fitted.corpus.held_out_trades)
    sizes = set()
    for trade in fitted.corpus.held_out_trades:
        before = set(trade.team1_before_player_ids)
        after = set(trade.team1_after_player_ids)
        outgoing = len(before.difference(after))
        incoming = len(after.difference(before))
        if outgoing == incoming:
            sizes.add(outgoing)
    return ids, tuple(sorted(sizes))


def _heldout_ids(value):
    if isinstance(value, (str, bytes)):
        raise ValueError("held_out_trade_ids must be a collection of identifiers")
    try:
        rows = tuple(value)
    except TypeError:
        raise ValueError(
            "held_out_trade_ids must be a collection of identifiers"
        ) from None
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ValueError("held_out_trade_ids must contain non-empty strings")
    normalized = tuple(sorted(item.strip() for item in rows))
    if len(set(normalized)) != len(normalized):
        raise ValueError("held_out_trade_ids must be distinct")
    return normalized


def _balanced_package_sizes(value):
    if isinstance(value, (str, bytes)):
        raise ValueError(
            "held_out_balanced_package_sizes must be positive integers"
        )
    try:
        rows = tuple(value)
    except TypeError:
        raise ValueError(
            "held_out_balanced_package_sizes must be positive integers"
        ) from None
    if any(type(item) is not int or item < 1 for item in rows):
        raise ValueError(
            "held_out_balanced_package_sizes must be positive integers"
        )
    return tuple(sorted(set(rows)))
