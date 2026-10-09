"""Another library in the same monorepo; FailGate must not pick its tests for confkit."""

from confkit import parse


def hosts(text: str) -> list[str]:
    return [s["host"] for s in parse(text).values() if "host" in s]
