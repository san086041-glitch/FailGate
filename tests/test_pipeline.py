"""Pipeline 的异常路径：模块出错、超预算、分诊为非 bug 时的汇总评论。"""

from typing import Any

import httpx
from conftest import Harness, _harness, issue_event, make_settings
from fake_llm import INTAKE_OK, TRIAGE_OK


async def _only_case(h: Harness) -> dict[str, Any]:
    cases = (await h.client.get("/api/cases")).json()
    return (await h.client.get(f"/api/cases/{cases[0]['id']}")).json()


async def test_skill_error_is_recorded_and_case_stays(harness: Harness):
    harness.llm.queue("intake", httpx.Response(400, text="invalid api key"))
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.failgate.worker.drain()
    case = await _only_case(harness)
    assert case["state"] == "INTAKE"
    assert case["runs"][0]["status"] == "error" and "HTTP 400" in case["runs"][0]["error"]
    assert case["effects"] == []


async def test_budget_exceeded_moves_case_to_failed(tmp_path):
    async for h in _harness(make_settings(tmp_path, case_budget_usd=0.0)):
        await h.send("issues", issue_event("opened"), "d-1")
        await h.failgate.worker.drain()
        case = await _only_case(h)
        assert case["state"] == "FAILED"
        assert case["runs"] == [] and case["transitions"][-1]["event"] == "budget.exceeded"


async def test_question_summary_does_not_ask_for_repro_info(harness: Harness):
    harness.llm.queue("intake", {**INTAKE_OK, "language": "en", "missing": ["repro_steps"]})
    harness.llm.queue(
        "triage",
        {**TRIAGE_OK, "type": "question", "labels": ["question"], "rationale": "How-to."},
    )
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.failgate.worker.drain()
    case = await _only_case(harness)
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    body = summary["payload"]["body"]
    assert "acceptance report" in body and "`question`" in body
    assert "please add" not in body


async def test_low_confidence_triage_does_not_propose_labels(harness: Harness):
    harness.llm.queue("triage", {**TRIAGE_OK, "confidence": 0.4})
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.failgate.worker.drain()
    case = await _only_case(harness)
    assert [e["action"] for e in case["effects"]] == ["upsert_summary"]
