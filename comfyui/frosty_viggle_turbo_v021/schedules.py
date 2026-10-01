"""Raw Turbo nodes: upstream examples and extensions for user-selected counts."""

EXAMPLES = {
    5: [1.0, 0.875, 0.75, 0.5, 0.25],
    6: [1.0, 0.9375, 0.875, 0.75, 0.5, 0.25],
    7: [1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25],
}


def raw_nodes(steps):
    if type(steps) is not int or steps < 1:
        raise ValueError("steps must be a positive integer")
    if steps in EXAMPLES:
        return list(EXAMPLES[steps])
    if steps >= 8:
        high_count = steps - 3
        return [1.0 - 0.125 * i / (high_count - 1) for i in range(high_count)] + [0.75, 0.5, 0.25]
    if steps == 1:
        return [1.0]
    # Fewer than five steps cannot retain all upstream anchors. Permit the
    # requested count by resampling the six-step nodes, without a quality claim.
    base = EXAMPLES[6]
    result = []
    for i in range(steps):
        position = i * (len(base) - 1) / (steps - 1)
        left = min(int(position), len(base) - 2)
        weight = position - left
        result.append(base[left] * (1 - weight) + base[left + 1] * weight)
    return result
