"""Coverage-aware selection from bounded calibration experiment candidates."""

from collections import defaultdict, deque
from math import fsum, sqrt

from ._calibration_experiment_design import ExperimentCandidate, candidate_key
from .trade_space import TeamRoster


def select_training_candidates(candidates, total):
    """Choose diverse atomic rows while round-robining counterparties."""

    if len(candidates) < total:
        raise ValueError(
            f"only {len(candidates)} distinct one-player training perturbations "
            f"are available; requested training needs {total}"
        )
    return _diverse_selection(candidates, total, (), ())


def select_holdout_candidates(
    candidates: tuple[ExperimentCandidate, ...],
    total: int,
    *,
    blocked_candidates: tuple[ExperimentCandidate, ...],
    counterparties: tuple[str, ...],
    by_team: dict[str, TeamRoster],
    primary_team_id: str,
) -> tuple[ExperimentCandidate, ...]:
    """Choose blind rows with explicit team and package-size coverage."""

    blocked_signatures = {row.signature for row in blocked_candidates}
    blocked_keys = {candidate_key(row) for row in blocked_candidates}
    training_rosters = {frozenset(roster.player_ids) for roster in by_team.values()}
    for row in blocked_candidates:
        training_rosters.update(_after_rosters(row, by_team, primary_team_id))
    eligible = tuple(
        row
        for row in candidates
        if row.signature not in blocked_signatures
        and candidate_key(row) not in blocked_keys
        and not training_rosters.intersection(
            _after_rosters(row, by_team, primary_team_id)
        )
    )
    distinct_signatures = {row.signature for row in eligible}
    if len(distinct_signatures) < total:
        raise ValueError(
            f"only {len(distinct_signatures)} distinct balanced blind perturbations "
            f"are available after training; requested holdouts need {total}"
        )
    package_sizes = _size_coverage(
        tuple(sorted({row.package_size for row in eligible})), total
    )
    selected = _diverse_selection(eligible, total, counterparties, package_sizes)
    missing_teams = set(counterparties).difference(
        row.team2_id for row in selected
    )
    if missing_teams:
        raise ValueError(
            f"blind holdout selection did not cover team {min(missing_teams)!r}"
        )
    missing_sizes = set(package_sizes).difference(
        row.package_size for row in selected
    )
    if missing_sizes:
        raise ValueError(
            "blind holdout selection did not cover balanced package size "
            f"{min(missing_sizes)}"
        )
    return selected


def _after_rosters(candidate, by_team, primary_team_id):
    before1 = set(by_team[primary_team_id].player_ids)
    before2 = set(by_team[candidate.team2_id].player_ids)
    return {
        frozenset(before1.difference(candidate.team1_gives).union(candidate.team2_gives)),
        frozenset(before2.difference(candidate.team2_gives).union(candidate.team1_gives)),
    }


def _size_coverage(available_sizes, holdout_count):
    priority = tuple(
        size for size in (2, 3, 4) if size in available_sizes
    ) + tuple(size for size in available_sizes if size not in (2, 3, 4))
    return priority[:holdout_count]


def _diverse_selection(candidates, total, required_teams, required_sizes):
    scales = tuple(
        max(abs(row.signature[index]) for row in candidates)
        for index in range(len(candidates[0].signature))
    )
    ordered = sorted(
        candidates,
        key=lambda row: (-_norm(row.signature, scales), candidate_key(row)),
    )
    selected, selected_keys, selected_signatures = [], set(), set()
    pending_sizes = deque(required_sizes)
    for team_id in required_teams:
        preferred_size = pending_sizes.popleft() if pending_sizes else None
        row = _first_available(
            ordered,
            selected_keys,
            selected_signatures,
            lambda candidate: candidate.team2_id == team_id
            and (preferred_size is None or candidate.package_size == preferred_size),
        )
        if row is None and preferred_size is not None:
            pending_sizes.appendleft(preferred_size)
            row = _first_available(
                ordered,
                selected_keys,
                selected_signatures,
                lambda candidate: candidate.team2_id == team_id,
            )
        _append(row, selected, selected_keys, selected_signatures)

    covered_sizes = {row.package_size for row in selected}
    for size in (*pending_sizes, *required_sizes):
        if size in covered_sizes or len(selected) >= total:
            continue
        row = _first_available(
            ordered,
            selected_keys,
            selected_signatures,
            lambda candidate, wanted=size: candidate.package_size == wanted,
        )
        if _append(row, selected, selected_keys, selected_signatures):
            covered_sizes.add(size)

    basis = _RowBasis(len(scales))
    for row in selected:
        basis.add(_scaled(row.signature, scales))
    for row in ordered:
        if len(selected) >= total:
            break
        if (
            candidate_key(row) not in selected_keys
            and row.signature not in selected_signatures
            and basis.add(_scaled(row.signature, scales))
        ):
            _append(row, selected, selected_keys, selected_signatures)

    groups = defaultdict(deque)
    for row in ordered:
        groups[(row.package_size, row.team2_id)].append(row)
    while len(selected) < total:
        made_progress = False
        for group in sorted(groups):
            if len(selected) >= total:
                break
            row = _next_available(groups[group], selected_keys, selected_signatures)
            made_progress |= _append(
                row, selected, selected_keys, selected_signatures
            )
        if not made_progress:
            raise ValueError("calibration candidate selection was exhausted")
    return tuple(selected)


def _first_available(ordered, selected_keys, selected_signatures, predicate):
    return next(
        (
            row
            for row in ordered
            if candidate_key(row) not in selected_keys
            and row.signature not in selected_signatures
            and predicate(row)
        ),
        None,
    )


def _next_available(queue, selected_keys, selected_signatures):
    while queue:
        row = queue.popleft()
        if (
            candidate_key(row) not in selected_keys
            and row.signature not in selected_signatures
        ):
            return row
    return None


def _append(row, selected, selected_keys, selected_signatures):
    if row is None:
        return False
    selected.append(row)
    selected_keys.add(candidate_key(row))
    selected_signatures.add(row.signature)
    return True


class _RowBasis:
    def __init__(self, width):
        self.width = width
        self.rows = {}

    def add(self, raw):
        row = list(raw)
        for pivot in sorted(self.rows):
            factor = row[pivot]
            if factor:
                basis = self.rows[pivot]
                for index in range(pivot, self.width):
                    row[index] -= factor * basis[index]
        pivot = next(
            (index for index, value in enumerate(row) if abs(value) > 1e-10),
            None,
        )
        if pivot is None:
            return False
        divisor = row[pivot]
        for index in range(pivot, self.width):
            row[index] /= divisor
        self.rows[pivot] = row
        return True


def _scaled(signature, scales):
    return tuple(
        0.0 if scale == 0 else value / scale
        for value, scale in zip(signature, scales)
    )


def _norm(signature, scales):
    return sqrt(fsum(value * value for value in _scaled(signature, scales)))
