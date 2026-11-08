"""Deterministic force-directed layout for networks, in two or three dimensions.

A frontend with its own layout engine can ignore these positions. One without can draw
the graph directly. The layout is reproducible: nodes start evenly spread (a circle in 2D,
a Fibonacci sphere in 3D) in a fixed order, so the same graph always gets the same picture,
which also keeps the response deterministic.

This is the Fruchterman-Reingold method: linked nodes attract, all nodes repel, and the
step size cools each round. Edge weight strengthens attraction, so entities that share
many trials sit close together. A weak pull toward the center keeps separate components
near each other. Without it, a disconnected pair drifts away and, once positions are
scaled to fit within -1 to 1, squeezes the main cluster into a corner.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Position = tuple[float, ...]

ROUNDS = 150
GRAVITY = 0.6


def force_layout(
    node_ids: Sequence[str], edges: Sequence[tuple[str, str, float]], dimensions: int
) -> dict[str, Position]:
    count = len(node_ids)
    if count == 0:
        return {}
    if count == 1:
        return {node_ids[0]: (0.0,) * dimensions}
    positions = [list(_start(i, count, dimensions)) for i in range(count)]
    index = {node: i for i, node in enumerate(node_ids)}
    springs = [
        (index[a], index[b], math.log1p(weight))
        for a, b, weight in edges
        if a in index and b in index
    ]
    ideal = (1.0 / count) ** (1.0 / dimensions)
    step = 0.1
    for _ in range(ROUNDS):
        moves = [[0.0] * dimensions for _ in range(count)]
        for i in range(count):
            for j in range(i + 1, count):
                delta = [positions[i][k] - positions[j][k] for k in range(dimensions)]
                distance = max(math.sqrt(sum(d * d for d in delta)), 1e-6)
                push = ideal * ideal / distance
                for k in range(dimensions):
                    moves[i][k] += delta[k] / distance * push
                    moves[j][k] -= delta[k] / distance * push
        for i, j, strength in springs:
            delta = [positions[i][k] - positions[j][k] for k in range(dimensions)]
            distance = max(math.sqrt(sum(d * d for d in delta)), 1e-6)
            pull = distance * distance / ideal * strength
            for k in range(dimensions):
                moves[i][k] -= delta[k] / distance * pull
                moves[j][k] += delta[k] / distance * pull
        for i in range(count):
            for k in range(dimensions):
                moves[i][k] -= positions[i][k] * GRAVITY / ideal
            length = max(math.sqrt(sum(m * m for m in moves[i])), 1e-9)
            for k in range(dimensions):
                positions[i][k] += moves[i][k] / length * min(length, step)
        step *= 0.97
    return _fit(node_ids, positions, dimensions)


def _start(i: int, count: int, dimensions: int) -> Position:
    if dimensions == 2:
        angle = 2 * math.pi * i / count
        return (math.cos(angle), math.sin(angle))
    golden = math.pi * (3 - math.sqrt(5))
    y = 1 - 2 * (i + 0.5) / count
    radius = math.sqrt(1 - y * y)
    return (math.cos(golden * i) * radius, y, math.sin(golden * i) * radius)


def _fit(
    node_ids: Sequence[str], positions: list[list[float]], dimensions: int
) -> dict[str, Position]:
    centers = [sum(p[k] for p in positions) / len(positions) for k in range(dimensions)]
    extent = max(abs(p[k] - centers[k]) for p in positions for k in range(dimensions)) or 1.0
    return {
        node: tuple(round((p[k] - centers[k]) / extent, 4) for k in range(dimensions))
        for node, p in zip(node_ids, positions, strict=True)
    }
