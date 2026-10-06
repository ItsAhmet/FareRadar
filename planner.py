"""Season planner: pick one getaway per free window under a total budget.

Objective: as many trips as possible, then the best score (cheapest total,
best window fit, or sunniest). Optional rule: never the same destination
twice. For up to `EXACT_MAX_WINDOWS` windows the search is exhaustive over
the top candidates per window; beyond that a greedy pass fills windows in
order of value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import groupby

from optimizer import Itinerary, Window

EXACT_MAX_WINDOWS = 10
CANDIDATES_PER_WINDOW = 6


@dataclass
class Plan:
    trips: list[Itinerary] = field(default_factory=list)
    budget: float = 0.0
    objective: str = "price"

    @property
    def total_cost(self) -> float:
        return round(sum(t.total_price for t in self.trips), 2)

    @property
    def remaining(self) -> float:
        return round(self.budget - self.total_cost, 2)

    @property
    def destinations(self) -> list[str]:
        return [t.destination for t in self.trips]


def _score(it: Itinerary, objective: str) -> float:
    """Higher is better."""
    if objective == "fit":
        return it.fit * 1000 - it.total_price
    if objective == "sun":
        return (it.sun_score or 0.0) * 1000 - it.total_price
    return -it.total_price


def candidates_by_window(itineraries: list[Itinerary], objective: str) -> list[tuple[Window, list[Itinerary]]]:
    """Per window, the best package per destination, best first, top K."""
    ordered = sorted(itineraries, key=lambda i: (i.window.start, -_score(i, objective)))
    out = []
    for window, group in groupby(ordered, key=lambda i: i.window):
        best_per_dest: dict[str, Itinerary] = {}
        for it in group:
            best_per_dest.setdefault(it.destination, it)
        picks = sorted(best_per_dest.values(), key=lambda i: -_score(i, objective))[:CANDIDATES_PER_WINDOW]
        out.append((window, picks))
    return out


def plan_season(
    itineraries: list[Itinerary],
    budget: float,
    max_trips: int | None = None,
    no_repeat: bool = True,
    objective: str = "price",
) -> Plan:
    windows = candidates_by_window(itineraries, objective)
    cap = max_trips if max_trips is not None else len(windows)
    if len(windows) <= EXACT_MAX_WINDOWS:
        trips = _exact(windows, budget, cap, no_repeat, objective)
    else:
        trips = _greedy(windows, budget, cap, no_repeat, objective)
    return Plan(trips=trips, budget=budget, objective=objective)


def _exact(windows, budget, cap, no_repeat, objective) -> list[Itinerary]:
    best: tuple[int, float, list[Itinerary]] = (0, float("-inf"), [])

    def dfs(i: int, chosen: list[Itinerary], spent: float, used: set[str], score: float) -> None:
        nonlocal best
        key = (len(chosen), score)
        if key > best[:2]:
            best = (len(chosen), score, list(chosen))
        if i == len(windows) or len(chosen) >= cap:
            return
        # Upper bound: even taking every remaining window can't beat best count → prune.
        if len(chosen) + (len(windows) - i) < best[0]:
            return
        _, picks = windows[i]
        for it in picks:
            if spent + it.total_price > budget:
                continue
            if no_repeat and it.destination in used:
                continue
            chosen.append(it)
            dfs(i + 1, chosen, spent + it.total_price, used | {it.destination}, score + _score(it, objective))
            chosen.pop()
        dfs(i + 1, chosen, spent, used, score)  # skip this window

    dfs(0, [], 0.0, set(), 0.0)
    return sorted(best[2], key=lambda t: t.window.start)


def _greedy(windows, budget, cap, no_repeat, objective) -> list[Itinerary]:
    """Cheapest-first fill so the trip count is maximised, then tidy up."""
    flat = sorted(((w, it) for w, picks in windows for it in picks), key=lambda x: x[1].total_price)
    chosen: dict[Window, Itinerary] = {}
    used: set[str] = set()
    spent = 0.0
    for w, it in flat:
        if w in chosen or len(chosen) >= cap or spent + it.total_price > budget:
            continue
        if no_repeat and it.destination in used:
            continue
        chosen[w] = it
        used.add(it.destination)
        spent += it.total_price
    return sorted(chosen.values(), key=lambda t: t.window.start)
