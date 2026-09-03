"""Grouped slot matching with shared team-position capacities."""


def maximum_group_slot_fill(
    owned_groups, available_groups, slots, position_capacities
):
    """Find a full matching quickly, falling back for mixed flexible groups."""

    slots, slot_capacities, index_map = _compress_slots(slots, owned_groups)
    owned_groups = _compress_owned_groups(owned_groups, index_map)
    target = sum(slot_capacities)
    available_signatures = tuple(sorted(available_groups))
    candidate_positions = tuple(sorted({
        (team, primary)
        for primary, _, round_limits in available_signatures
        for team, round_limit in enumerate(round_limits)
        if round_limit > 0 and position_capacities.get((team, primary), 0) > 0
    }))
    signature_sets = {
        team_position: tuple(
            set(eligible)
            for primary, eligible, round_limits in available_signatures
            if primary == team_position[1] and round_limits[team_position[0]] > 0
        )
        for team_position in candidate_positions
    }
    compatible_slots = {
        team_position: sum(
            slot_capacities[index]
            for index, (team, allowed) in enumerate(slots)
            if team == team_position[0]
            and any(eligible.intersection(allowed) for eligible in eligibility_sets)
        )
        for team_position, eligibility_sets in signature_sets.items()
    }
    direct_positions = {
        row
        for row in candidate_positions
        if position_capacities[row] >= compatible_slots[row]
    }
    capped_positions = tuple(row for row in candidate_positions if row not in direct_positions)
    total = _run_group_flow(
        owned_groups,
        available_groups,
        slots,
        position_capacities,
        signature_sets,
        direct_positions,
        capped_positions,
        slot_capacities,
        target,
    )
    mixed_capped_position = any(
        len({tuple(row) for row in signature_sets[position]}) > 1
        for position in capped_positions
    )
    if total == target or not mixed_capped_position:
        return total
    return target if _exact_full_matching(
        owned_groups, available_groups, slots, slot_capacities,
        position_capacities,
    ) else total


def _compress_slots(slots, owned_groups):
    compressed = []
    capacities = []
    indexes = {}
    index_map = []
    owned_signatures = tuple(sorted(owned_groups))
    for slot_index, (team, allowed) in enumerate(slots):
        ownership = tuple(
            group_index
            for group_index, edges in enumerate(owned_signatures)
            if slot_index in edges
        )
        signature = team, tuple(allowed), ownership
        group_index = indexes.get(signature)
        if group_index is None:
            group_index = len(compressed)
            indexes[signature] = group_index
            compressed.append((team, tuple(allowed)))
            capacities.append(0)
        capacities[group_index] += 1
        index_map.append(group_index)
    return tuple(compressed), tuple(capacities), tuple(index_map)


def _compress_owned_groups(owned_groups, index_map):
    compressed = {}
    for signature, count in owned_groups.items():
        edges = tuple(sorted({index_map[index] for index in signature}))
        if edges:
            compressed[edges] = compressed.get(edges, 0) + count
    return compressed


def _run_group_flow(
    owned_groups,
    available_groups,
    slots,
    position_capacities,
    signature_sets,
    direct_positions,
    capped_positions,
    slot_capacities,
    target,
):
    owned_signatures = tuple(sorted(owned_groups))
    available_signatures = tuple(sorted(available_groups))
    group_count = len(owned_signatures) + len(available_signatures)
    source = 0
    group_start = 1
    position_start = group_start + group_count
    position_out_start = position_start + len(capped_positions)
    slot_start = position_out_start + len(capped_positions)
    sink = slot_start + len(slots)
    graph = [[] for _ in range(sink + 1)]

    def edge(left, right, capacity):
        graph[left].append([right, len(graph[right]), capacity])
        graph[right].append([left, len(graph[left]) - 1, 0])

    for group_index, signature in enumerate(owned_signatures):
        node = group_start + group_index
        supply = owned_groups[signature]
        edge(
            source,
            node,
            min(supply, sum(slot_capacities[index] for index in signature)),
        )
        for slot_index in signature:
            edge(
                node,
                slot_start + slot_index,
                min(supply, slot_capacities[slot_index]),
            )
    position_nodes = {
        value: position_start + index for index, value in enumerate(capped_positions)
    }
    for group_index, signature in enumerate(
        available_signatures, len(owned_signatures)
    ):
        primary, eligible, round_limits = signature
        node = group_start + group_index
        edge(source, node, available_groups[signature])
        for team, round_limit in enumerate(round_limits):
            if round_limit <= 0:
                continue
            team_position = team, primary
            if team_position in direct_positions:
                for slot_index, (slot_team, allowed) in enumerate(slots):
                    if slot_team == team and set(eligible).intersection(allowed):
                        edge(
                            node,
                            slot_start + slot_index,
                            min(
                                available_groups[signature],
                                slot_capacities[slot_index],
                            ),
                        )
            elif team_position in position_nodes:
                edge(
                    node,
                    position_nodes[team_position],
                    min(available_groups[signature], round_limit),
                )
    for index, team_position in enumerate(capped_positions):
        node = position_nodes[team_position]
        output_node = position_out_start + index
        edge(node, output_node, position_capacities[team_position])
        for slot_index, (team, allowed) in enumerate(slots):
            if team == team_position[0] and all(
                eligible.intersection(allowed)
                for eligible in signature_sets[team_position]
            ):
                edge(
                    output_node,
                    slot_start + slot_index,
                    slot_capacities[slot_index],
                )
    for slot_index in range(len(slots)):
        edge(
            slot_start + slot_index,
            sink,
            slot_capacities[slot_index],
        )
    return _dinic(graph, source, sink, target)


def _dinic(graph, source, sink, target):
    total = 0
    while total < target:
        levels = [-1] * len(graph)
        levels[source] = 0
        queue = [source]
        for node in queue:
            for adjacent, _, capacity in graph[node]:
                if capacity and levels[adjacent] < 0:
                    levels[adjacent] = levels[node] + 1
                    queue.append(adjacent)
        if levels[sink] < 0:
            break
        cursors = [0] * len(graph)

        def send(node, capacity):
            if node == sink:
                return capacity
            while cursors[node] < len(graph[node]):
                item = graph[node][cursors[node]]
                adjacent, reverse, edge_capacity = item
                if edge_capacity and levels[adjacent] == levels[node] + 1:
                    amount = send(adjacent, min(capacity, edge_capacity))
                    if amount:
                        item[2] -= amount
                        graph[adjacent][reverse][2] += amount
                        return amount
                cursors[node] += 1
            return 0

        while (amount := send(source, target - total)):
            total += amount
    return total


def _exact_full_matching(
    owned_groups, available_groups, slots, slot_capacities,
    position_capacities,
):
    """Resolve the uncommon mixed-eligibility/capped case without relaxation."""

    owned_signatures = tuple(sorted(owned_groups))
    available_signatures = tuple(sorted(available_groups))
    counts = tuple(
        min(sum(slot_capacities), groups[signature])
        for groups, signatures in (
            (owned_groups, owned_signatures),
            (available_groups, available_signatures),
        )
        for signature in signatures
    )
    cap_keys = tuple(sorted(
        key for key, value in position_capacities.items() if value > 0
    ))
    cap_indexes = {key: index for index, key in enumerate(cap_keys)}
    caps = tuple(position_capacities[key] for key in cap_keys)
    options = [[] for _ in slots]
    for group_index, signature in enumerate(owned_signatures):
        for slot_index in signature:
            options[slot_index].append((group_index, -1))
    for offset, signature in enumerate(
        available_signatures, len(owned_signatures)
    ):
        primary, eligible, round_limits = signature
        eligible = set(eligible)
        for slot_index, (team, allowed) in enumerate(slots):
            cap_index = cap_indexes.get((team, primary))
            if (
                round_limits[team]
                and cap_index is not None
                and eligible.intersection(allowed)
            ):
                options[slot_index].append((offset, cap_index))
    if any(not row for row in options):
        return False

    failed = set()
    def search(open_slots, remaining, remaining_caps):
        if not any(open_slots):
            return True
        state = open_slots, remaining, remaining_caps
        if state in failed:
            return False
        chosen_slot, candidates = _most_constrained_slot(
            open_slots, options, remaining, remaining_caps
        )
        if not candidates:
            failed.add(state)
            return False
        for group_index, cap_index in candidates:
            next_counts = list(remaining)
            next_counts[group_index] -= 1
            next_caps = list(remaining_caps)
            if cap_index >= 0:
                next_caps[cap_index] -= 1
            next_slots = list(open_slots)
            next_slots[chosen_slot] -= 1
            if search(
                tuple(next_slots), tuple(next_counts), tuple(next_caps)
            ):
                return True
        failed.add(state)
        return False

    return search(slot_capacities, counts, caps)


def _most_constrained_slot(open_slots, options, remaining, remaining_caps):
    chosen_slot = -1
    candidates = None
    for slot, slot_count in enumerate(open_slots):
        if not slot_count:
            continue
        available = tuple(
            option
            for option in options[slot]
            if remaining[option[0]]
            and (option[1] < 0 or remaining_caps[option[1]])
        )
        if not available:
            return slot, ()
        if candidates is None or len(available) < len(candidates):
            chosen_slot, candidates = slot, available
            if len(candidates) == 1:
                break
    return chosen_slot, candidates


__all__ = ("maximum_group_slot_fill",)
