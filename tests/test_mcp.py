"""本地 MCP 服务（ADR 0033）。

本地仓库 → 源码包、证据库、长任务、MCP 协议层；最后一个真实 Docker 场景。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from failgate.mcp_server import local
from failgate.mcp_server.server import build_server, fix_task
from failgate.mcp_server.store import EvidenceStore, JobManager, LocalEvidence
from failgate.repro.judge import RunRecord, Verdict, VerdictKind
from failgate.repro.l2 import pytest_argv
from failgate.verify.receipt import build_receipt

FIXTURE = Path(__file__).parent.parent / "fixtures" / "repos" / "bug-keyerror"
EXAM_PATH = "tests/test_failgate_issue_0.py"
KEYERROR_TEST = (
    "from confkit import parse\n\n\n"
    "def test_keys_before_first_section_go_to_default():\n"
    '    assert parse("name = demo\\n[server]\\nhost = x\\n")["DEFAULT"] == {"name": "demo"}\n'
)


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                          *args], capture_output=True, text=True, check=True)
    return out.stdout


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE / "repo", root)
    (root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------- 本地仓库 → 源码包


def test_worktree_includes_uncommitted_and_untracked_but_not_ignored(tmp_path: Path):
    root = make_repo(tmp_path)
    assert local.repo_root(root / "confkit") == local.repo_root(root)
    assert not local.is_dirty(root)
    # 用字节写：Windows 上 write_text 会把 \n 写成 \r\n（打包按原始字节，不改换行）
    (root / "confkit" / "parser.py").write_bytes(b"X = 1\n")  # 未提交的修改
    (root / "confkit" / "new.py").write_bytes(b"Y = 2\n")  # 未跟踪
    (root / "debug.log").write_bytes(b"noise\n")  # 被忽略
    (root / "build").mkdir()
    (root / "build" / "out.py").write_bytes(b"Z = 3\n")
    assert local.is_dirty(root)
    tree = local.worktree_tree(root)
    files = tree.read_files(lambda _: True)
    assert files["confkit/parser.py"] == "X = 1\n" and files["confkit/new.py"] == "Y = 2\n"
    assert "debug.log" not in files and "build/out.py" not in files
    assert tree.top_dir == "src" and tree.sha.endswith("-worktree")
    assert local.worktree_tree(root).sha == tree.sha  # 内容不变，摘要不变（环境缓存能复用）

    base = local.ref_tree(root, "HEAD")
    head_sha, _ = local.resolve(root, "HEAD")
    assert base.sha == head_sha and "X = 1" not in base.read_files(lambda _: True)[
        "confkit/parser.py"]

    diff = {f.filename: f for f in local.tree_diff(base, tree)}
    assert set(diff) == {"confkit/parser.py", "confkit/new.py", ".gitignore"} - {".gitignore"}
    assert diff["confkit/new.py"].status == "added"
    assert diff["confkit/parser.py"].status == "modified"
    patch = diff["confkit/parser.py"].patch or ""
    assert patch.startswith("@@") and "+X = 1" in patch  # 和 GitHub 一样不带 ---/+++ 头
    (root / "tests" / "conftest.py").unlink()
    removed = {f.filename: f.status for f in local.tree_diff(base, local.worktree_tree(root))}
    assert removed["tests/conftest.py"] == "removed"


def test_local_repo_errors(tmp_path: Path):
    with pytest.raises(local.LocalRepoError):
        local.repo_root(tmp_path)  # 不是 git 仓库
    with pytest.raises(local.LocalRepoError):
        local.repo_root(tmp_path / "nope")
    root = make_repo(tmp_path)
    with pytest.raises(local.LocalRepoError, match="找不到提交|失败"):
        local.ref_tree(root, "no-such-ref")


# ---------------------------------------------------------------- 证据库与长任务

VERDICT = Verdict(kind=VerdictKind.REPRODUCED, reason="4 次", match=0.92,
                  match_method="signature", runs=4, fail_rate=1.0,
                  records=[RunRecord(exit_code=1, same_failure=True)] * 4)


def evidence(root: Path, code: str = KEYERROR_TEST, **kw: Any) -> LocalEvidence:
    receipt = build_receipt(
        repo=f"local/{root.name}", issue=0, level="L2", mode="source", test_path=EXAM_PATH,
        code=code, package="confkit", command=pytest_argv(EXAM_PATH), verdict=VERDICT,
        python="3.12")
    signed = receipt.signed_dict()
    sha, _ = local.resolve(root, "HEAD")
    data = {"evidence_id": signed["evidence_id"], "repo_path": str(root), "base_sha": sha,
            "dirty": False, "title": "KeyError: None", "body": "parse() crashes",
            "import_name": "confkit", "receipt": signed, "code": code,
            "created_at": "2026-10-01T00:00:00Z", **kw}
    return LocalEvidence(**data)


def test_store_save_get_prefix_and_append_only(tmp_path: Path):
    root = make_repo(tmp_path)
    store = EvidenceStore(tmp_path / "home")
    ev = evidence(root)
    store.save(ev)
    assert store.get(ev.evidence_id) == ev
    assert store.get(ev.evidence_id[:10]) == ev
    with pytest.raises(KeyError, match="至少"):
        store.get(ev.evidence_id[:4])
    with pytest.raises(KeyError):
        store.get("../../etc/passwd")
    with pytest.raises(FileExistsError):
        store.save(ev)
    assert [e.evidence_id for e in store.all(str(root))] == [ev.evidence_id]
    assert store.all(str(tmp_path / "other")) == []
    assert store.problems(ev) == []
    assert store.problems(ev.model_copy(update={"code": "def test_x(): pass\n"}))
    exam = ev.exam()
    assert exam.test_path == EXAM_PATH and exam.module == "confkit" and exam.python == "3.12"


async def test_job_manager_done_failed_and_unknown():
    jobs = JobManager()

    async def ok(progress: Any) -> dict[str, Any]:
        progress("一半了")
        await asyncio.sleep(0)
        return {"x": 1}

    async def boom(progress: Any) -> dict[str, Any]:
        raise RuntimeError("坏了")

    j1, j2 = jobs.start("a", ok), jobs.start("b", boom)
    v1 = (await jobs.wait(j1.job_id, 5)).view()
    assert v1["status"] == "done" and v1["result"] == {"x": 1} and v1["progress"] == ["一半了"]
    v2 = (await jobs.wait(j2.job_id, 5)).view()
    assert v2["status"] == "failed" and "坏了" in v2["error"]
    with pytest.raises(KeyError):
        await jobs.wait("nope", 0)


# ---------------------------------------------------------------- MCP 协议层（假引擎）


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def reproduce(self, repo_path: str, title: str, body: str, package: str,
                        import_name: str | None, python: str | None,
                        progress: Any) -> dict[str, Any]:
        self.calls.append(("reproduce", (repo_path, title, package)))
        progress("写测试中")
        return {"reproduced": True, "evidence_id": "e" * 32}

    async def acceptance(self, repo_path: str, ev: LocalEvidence, progress: Any) -> dict[str, Any]:
        self.calls.append(("acceptance", ev.evidence_id))
        return {"passed": True}

    async def verify(self, repo_path: str, ev: LocalEvidence, base_ref: str | None,
                     strength: bool, progress: Any) -> dict[str, Any]:
        self.calls.append(("verify", (ev.evidence_id, base_ref, strength)))
        return {"verdict": "VERIFIED"}


def _data(res: Any) -> Any:
    assert not res.is_error, res
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


async def test_mcp_tools_end_to_end_with_fake_engine(tmp_path: Path):
    root = make_repo(tmp_path)
    store = EvidenceStore(tmp_path / "home")
    ev = evidence(root)
    store.save(ev)
    engine = FakeEngine()
    server = build_server(engine, store)
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"reproduce_issue", "run_acceptance_test", "verify_fix", "get_job",
                         "get_fix_task", "list_evidence"}

        started = _data(await client.call_tool("reproduce_issue", {
            "repo_path": str(root), "title": "crash", "body": "KeyError", "package": "confkit"}))
        assert started["status"] == "running" and "get_job" in started["next"]
        job = _data(await client.call_tool("get_job", {"job_id": started["job_id"],
                                                       "wait_seconds": 5}))
        assert job["status"] == "done" and job["result"]["reproduced"] is True
        assert job["progress"] == ["写测试中"]

        v = _data(await client.call_tool("verify_fix", {
            "repo_path": str(root), "evidence_id": ev.evidence_id[:10], "base_ref": "HEAD~0"}))
        done = _data(await client.call_tool("get_job", {"job_id": v["job_id"],
                                                        "wait_seconds": 5}))
        assert done["result"] == {"verdict": "VERIFIED"}
        assert ("verify", (ev.evidence_id, "HEAD~0", False)) in engine.calls

        task = (await client.call_tool("get_fix_task", {"evidence_id": ev.evidence_id}))
        text = task.content[0].text
        assert EXAM_PATH in text and ev.receipt["test_sha256"] in text
        assert "不要修改这份测试" in text

        listed = _data(await client.call_tool("list_evidence", {"repo_path": str(root)}))
        assert [e["evidence_id"] for e in listed] == [ev.evidence_id]

        res = await client.read_resource(f"failgate://evidence/{ev.evidence_id}")
        body = json.loads(res.contents[0].text)
        assert body["test_code"] == KEYERROR_TEST
        assert body["receipt"]["receipt_sha256"] == ev.receipt["receipt_sha256"]

        missing = await client.call_tool("verify_fix", {"repo_path": str(root),
                                                        "evidence_id": "0123456789"})
        assert missing.is_error


async def test_tampered_evidence_file_is_refused(tmp_path: Path):
    root = make_repo(tmp_path)
    store = EvidenceStore(tmp_path / "home")
    ev = evidence(root)
    store.save(ev)
    # 有人直接改了证据文件里的考卷
    path = store.dir / f"{ev.evidence_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["code"] = "def test_x():\n    pass\n"
    path.write_text(json.dumps(data), encoding="utf-8")
    engine = FakeEngine()
    async with Client(build_server(engine, store)) as client:
        res = await client.call_tool("verify_fix", {"repo_path": str(root),
                                                    "evidence_id": ev.evidence_id})
        assert res.is_error and "对不上" in res.content[0].text
    assert engine.calls == []


def test_fix_task_card(tmp_path: Path):
    root = make_repo(tmp_path)
    text = fix_task(evidence(root))
    assert text.startswith("# 修复任务：KeyError: None")
    assert f"python -m pytest {EXAM_PATH}" in text and "verify_fix" in text


# ---------------------------------------------------------------- 真实 Docker（不调 LLM）


@pytest.fixture(scope="module")
def sandbox() -> Any:
    from failgate.repro.sandbox import DockerSandbox

    sb = DockerSandbox()
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


@pytest.mark.docker
def test_real_local_verify_fix_and_tampering(sandbox: Any, tmp_path: Path):
    """本地仓库：没修 → 考卷失败、驳回；修好 → 通过；修好但把考卷改成永远通过 → 驳回（防篡改）。"""
    from test_repro_l2 import OfflinePyPI

    from failgate.mcp_server.engine import FailGateEngine, Runtime
    from failgate.repro.envcache import EnvCache
    from failgate.repro.l2 import TestReproducer
    from failgate.settings import Settings

    root = make_repo(tmp_path)
    store = EvidenceStore(tmp_path / "home")
    ev = evidence(root)
    store.save(ev)
    tester = TestReproducer(sandbox, EnvCache(sandbox, tmp_path / "envcache.json"),
                            OfflinePyPI())  # type: ignore[arg-type]
    engine = FailGateEngine(Runtime(settings=Settings(), llm=None, tester=tester), store)

    def progress(_: str) -> None:
        pass

    # 还没修：考卷在当前工作区上失败；核验驳回
    acc = asyncio.run(engine.acceptance(str(root), ev, progress))
    assert acc["passed"] is False and acc["outcome"] == "failed_same", acc
    v = asyncio.run(engine.verify(str(root), ev, None, False, progress))
    assert v["verdict"] == "REFUTED", v["reasons"]

    # 修好（未提交）：考卷通过，三层核验通过
    shutil.copytree(FIXTURE / "fix", root, dirs_exist_ok=True)
    acc = asyncio.run(engine.acceptance(str(root), ev, progress))
    assert acc["passed"] is True, acc
    v = asyncio.run(engine.verify(str(root), ev, None, False, progress))
    assert v["verdict"] == "VERIFIED", (v["reasons"], v["verification"])
    assert v["changed_files"] == ["confkit/parser.py"]

    # 把考卷原样放进仓库：哈希一致，照样通过
    (root / EXAM_PATH).write_text(KEYERROR_TEST, encoding="utf-8")
    assert asyncio.run(engine.verify(str(root), ev, None, False, progress))["verdict"] == \
        "VERIFIED"
    # 改了考卷：防篡改驳回
    (root / EXAM_PATH).write_text("def test_keys_before_first_section_go_to_default():\n"
                                  "    assert True\n", encoding="utf-8")
    v = asyncio.run(engine.verify(str(root), ev, None, False, progress))
    assert v["verdict"] == "REFUTED", v["reasons"]


def test_tree_diff_ignores_line_ending_only_changes(tmp_path: Path):
    """Windows 上 autocrlf：工作区是 CRLF、git archive 是 LF，只有换行不同不算改动。"""
    root = make_repo(tmp_path)
    base = local.ref_tree(root, "HEAD")
    lf = base.read_files(lambda p: p == "confkit/parser.py")["confkit/parser.py"]
    crlf = base.overlay({"confkit/parser.py": lf.replace("\r\n", "\n").replace("\n", "\r\n")},
                        label="crlf")
    assert local.tree_diff(base, crlf) == []
    changed = base.overlay({"confkit/parser.py": lf + "# x\r\n"}, label="changed")
    assert [f.filename for f in local.tree_diff(base, changed)] == ["confkit/parser.py"]
