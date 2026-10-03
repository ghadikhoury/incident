"""Validated service dependency graph shared by the API and alarm processor."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ServiceNode:
    name: str
    display_name: str
    depends_on: tuple[str, ...]
    monitored: bool
    baseline_latency_ms: float | None


class DependencyGraph:
    def __init__(self, nodes: dict[str, ServiceNode]):
        self.nodes = nodes
        for node in nodes.values():
            for dependency in node.depends_on:
                if dependency not in nodes or dependency == node.name:
                    raise ValueError(f"invalid dependency {node.name} -> {dependency}")
        self._closure: dict[str, frozenset[str]] = {}
        visiting: set[str] = set()

        def visit(name: str) -> frozenset[str]:
            if name in visiting:
                raise ValueError(f"dependency cycle at {name}")
            if name not in self._closure:
                visiting.add(name)
                dependencies = set(nodes[name].depends_on)
                for dependency in nodes[name].depends_on:
                    dependencies.update(visit(dependency))
                visiting.remove(name)
                self._closure[name] = frozenset(dependencies)
            return self._closure[name]

        for name in nodes:
            visit(name)

    @classmethod
    def from_file(cls, path: Path) -> "DependencyGraph":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            raise ValueError("services.yaml must contain a services mapping")
        nodes = {}
        for name, fields in data["services"].items():
            if not isinstance(fields, dict):
                raise ValueError(f"service {name} must be a mapping")
            baseline = fields.get("baseline_latency_ms")
            if baseline is not None and (not isinstance(baseline, (int, float)) or baseline <= 0):
                raise ValueError(f"invalid latency baseline for {name}")
            nodes[name] = ServiceNode(
                name=name,
                display_name=fields["display_name"],
                depends_on=tuple(fields.get("depends_on", [])),
                monitored=fields.get("monitored", True),
                baseline_latency_ms=baseline,
            )
        return cls(nodes)

    def dependencies(self, name: str) -> frozenset[str]:
        return self._closure[name]

    def dependents(self, name: str) -> frozenset[str]:
        return frozenset(other for other in self.nodes if name in self._closure[other])

    def related(self, left: str, right: str) -> bool:
        """An ancestor and a descendant can belong to one failure cascade.

        Siblings such as payment and inventory share an upstream caller but are not
        evidence of the same failure by themselves.
        """
        return left == right or right in self._closure[left] or left in self._closure[right]

    def as_api(self) -> list[dict]:
        return [
            {
                "name": node.name,
                "display_name": node.display_name,
                "depends_on": list(node.depends_on),
                "monitored": node.monitored,
            }
            for node in self.nodes.values()
        ]


GRAPH = DependencyGraph.from_file(Path(__file__).with_name("services.yaml"))
