"""The one topo-sort, exercised at both levels it is used at."""

import pytest

from forman.topo import CycleError, ready_nodes, topo_sort


def test_orders_dependencies_first():
    graph = {"c": ["b"], "b": ["a"], "a": []}
    assert topo_sort(graph) == ["a", "b", "c"]


def test_ties_break_alphabetically_so_runs_are_reproducible():
    graph = {"b": [], "a": [], "c": []}
    assert topo_sort(graph) == ["a", "b", "c"]


def test_diamond():
    graph = {"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}
    order = topo_sort(graph)
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_dependencies_outside_the_graph_are_ignored():
    # A ticket blocked_by someone else's ticket is not ours to order.
    graph = {"TEAM-2": ["OTHER-9"], "TEAM-1": []}
    assert topo_sort(graph) == ["TEAM-1", "TEAM-2"]


def test_cycle_raises_with_the_offending_nodes():
    with pytest.raises(CycleError) as exc:
        topo_sort({"a": ["b"], "b": ["a"]})
    assert exc.value.remaining == ["a", "b"]


def test_subtask_level_usage():
    graph = {"T-1.01": [], "T-1.02": ["T-1.01"], "T-1.03": ["T-1.01"]}
    assert topo_sort(graph) == ["T-1.01", "T-1.02", "T-1.03"]


def test_ready_nodes_respects_satisfied_set():
    graph = {"T-1.01": [], "T-1.02": ["T-1.01"], "T-1.03": ["T-1.02"]}
    assert ready_nodes(graph, satisfied=set()) == ["T-1.01"]
    assert ready_nodes(graph, satisfied={"T-1.01"}) == ["T-1.02"]
    assert ready_nodes(graph, satisfied={"T-1.01", "T-1.02"}) == ["T-1.03"]


def test_ready_nodes_at_ticket_level_filters_unfinished_blockers():
    graph = {"TEAM-1": [], "TEAM-2": ["TEAM-1"]}
    assert ready_nodes(graph, satisfied=set()) == ["TEAM-1"]
