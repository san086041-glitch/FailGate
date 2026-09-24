from typing import Any


async def only_case(harness: Any, number: int | None = None) -> dict[str, Any]:
    """取某个 issue 对应的 Case 详情；不指定 number 时要求只有一个 Case。"""
    cases = (await harness.client.get("/api/cases")).json()
    if number is None:
        assert len(cases) == 1
        target = cases[0]
    else:
        target = next(c for c in cases if c["number"] == number)
    return (await harness.client.get(f"/api/cases/{target['id']}")).json()
