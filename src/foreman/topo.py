"""One topological sort, used at both levels of the pipeline.

Across tickets:  node = ticket identifier, deps = `blocked_by`
Within a ticket: node = sub-task id,      deps = `depends_on`

The sort is deterministic: ties break alphabetically, so the same input always
produces the same order. That matters because a re-run has to pick up where the
previous run left off.
"""

from __future__ import annotations


class CycleError(ValueError):
    """Raised when the dependency graph cannot be linearized."""

    def __init__(self, remaining: list[str]) -> None:
        self.remaining = sorted(remaining)
        super().__init__(f"dependency cycle among: {', '.join(self.remaining)}")


def topo_sort(graph: dict[str, list[str]]) -> list[str]:
    """Return nodes ordered so every node follows its dependencies.

    `graph` maps node -> list of nodes it depends on. Dependencies that are not
    themselves nodes in the graph are ignored: at ticket level a `blocked_by`
    pointing at someone else's ticket is not ours to order.
    """
    known = set(graph)
    deps = {node: {d for d in dependencies if d in known} for node, dependencies in graph.items()}

    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(node for node, d in remaining.items() if not (d - set(ordered)))
        if not ready:
            raise CycleError(list(remaining))
        ordered.extend(ready)
        for node in ready:
            del remaining[node]
    return ordered


def ready_nodes(graph: dict[str, list[str]], satisfied: set[str]) -> list[str]:
    """Nodes whose dependencies are all in `satisfied`, in deterministic order.

    Used to answer "which sub-task may run now" and "which ticket may be pulled
    now" with the same code path.
    """
    known = set(graph)
    out = []
    for node, dependencies in graph.items():
        if node in satisfied:
            continue
        if all(d in satisfied or d not in known for d in dependencies):
            out.append(node)
    return sorted(out)
