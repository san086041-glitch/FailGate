"""在 GitHub Actions 上跑回放评测（第二期 W2，ADR 0045）：环境下载走 GitHub 的机器，不走本机网络。

由 .github/workflows/repo-eval.yml 调用：推送 `eval-run/<名字>` 分支时
读 `eval/actions/<名字>.json`。

    {"repo": "langchain-ai/langchain", "package": "langchain-core", "subdir": "libs/core",
     "stage": "smoke" | "l2" | "checkup", "limit": 3, "offset": 0, "since": "2025-01-01",
     "budget_per_issue": 0.05, "backfill_limit": 6000, "smoke_refs": ["HEAD", "before:2025-06-01"]}

- smoke（$0，不调 LLM）：在给定提交上建环境（subdir + verify.json 的 test_deps）、跑预检，再跑一遍
  子目录的单元测试，看环境是否健康。
- l2：回填 issue → `replay l2`（开发集，允许据此调适配器）。
- checkup：`failgate checkup --accept-rule`（留出集：只跑一次，不调参）。

test_deps 一律取 `eval/datasets/<repo>/verify.json`，和本地核验同一份。花钱的阶段前后各打印一次
LLM 余额（只打印余额），报告里的花费按余额差核对，不按价格表。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPLAY_DB = "sqlite+aiosqlite:///eval/cache/replay.db"


def failgate(*args: str) -> None:
    cmd = [sys.executable, "-m", "failgate", *args]
    print("\n$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def balance() -> str:
    import httpx

    from failgate.settings import Settings

    s = Settings()
    if not s.llm_api_key:
        return "no LLM_API_KEY"
    try:
        r = httpx.get(s.llm_base_url.rstrip("/") + "/user/balance",
                      headers={"Authorization": f"Bearer {s.llm_api_key}"}, timeout=20)
        infos = r.json().get("balance_infos") or []
        return ", ".join(f"{i['currency']} {i['total_balance']}" for i in infos) or r.text[:200]
    except Exception as e:  # noqa: BLE001 — 只是记录，查不到不影响评测
        return f"balance unavailable: {type(e).__name__}"


async def smoke(spec: dict[str, Any], deps: list[str], out: Path) -> None:
    from failgate.cli import _env_cache
    from failgate.platforms.github_rest import GitHubRest
    from failgate.repro.config import PackageConfig
    from failgate.repro.l2 import RUN_PREFIXES, WORK_SRC, TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.sandbox import DockerSandbox
    from failgate.repro.source import fetch_github_tree
    from failgate.settings import Settings

    settings = Settings()
    sandbox = DockerSandbox.from_settings(settings)
    gh, pypi = GitHubRest(settings.github_token), PyPIClient(settings.pypi_url)
    tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi, run_timeout_s=300)
    cfg = PackageConfig(name=spec["package"], import_name=spec.get("import_name"),
                        subdir=spec.get("subdir"), test_deps=deps)
    rows: list[dict[str, Any]] = []
    try:
        for ref in spec.get("smoke_refs", ["HEAD"]):
            row: dict[str, Any] = {"ref": ref}
            t0 = time.monotonic()
            try:
                if ref.startswith("before:"):
                    day = datetime.fromisoformat(ref.split(":", 1)[1]).replace(tzinfo=UTC)
                    tree = await fetch_github_tree(gh, spec["repo"], before=day)
                else:
                    tree = await fetch_github_tree(gh, spec["repo"], ref)
                row.update(sha=tree.sha, committed_at=str(tree.committed_at),
                           tarball_mb=round(len(tree.tarball) / 1e6, 1))
                prep = await tester.prepare(cfg, tree, number=0)
                row.update(python=prep.python, pytest=prep.pytest, test_path=prep.test_path,
                           probe="ok", env_s=round(time.monotonic() - t0))
                ws = await tester.open_workspace(prep, "smoke")
                try:
                    unit = f"{WORK_SRC}/{prep.test_path.rsplit('/', 1)[0]}"
                    run = await sandbox.run(
                        prep.env.image, ws,
                        ["python", "-m", "pytest", unit, "-q", "-p", "no:cacheprovider",
                         f"--rootdir={WORK_SRC}", "--disable-socket", "--allow-unix-socket",
                         # 个别文件收集失败（缺不装的依赖）时继续跑其余的，看环境整体是否健康
                         "--continue-on-collection-errors"],
                        timeout_s=1200, allowed=RUN_PREFIXES)
                finally:
                    await sandbox.remove_workspace(ws)
                tail = run.output_tail(25)
                row.update(unit_exit=run.exit_code, unit_tail=tail)
                print(f"\n== {ref}: unit tests exit {run.exit_code}\n{tail}", flush=True)
            except Exception as e:  # noqa: BLE001 — 装不上本身就是结果
                row.update(probe="failed", error=f"{type(e).__name__}: {str(e)[:2000]}")
            row["total_s"] = round(time.monotonic() - t0)
            print(json.dumps({k: v for k, v in row.items() if k != "unit_tail"},
                             ensure_ascii=False), flush=True)
            rows.append(row)
    finally:
        await gh.aclose()
        await pypi.aclose()
    await asyncio.to_thread(out.write_text, json.dumps(rows, ensure_ascii=False, indent=1),
                            encoding="utf-8")


async def inject(spec: dict[str, Any], deps: list[str], out: Path) -> None:
    """break_other 漏判的定向复查（$0）：在 sha 上往 path 的 function 注入同样的 raise，
    干净的和注入的各跑一遍 tests，看仓库已有测试到底能不能发现这处回归。"""
    import ast

    from failgate.cli import _env_cache
    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay.verify_eval import _functions, inject_raise
    from failgate.repro.config import PackageConfig
    from failgate.repro.l2 import RUN_PREFIXES, WORK_SRC, TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.sandbox import DockerSandbox
    from failgate.repro.source import fetch_github_tree
    from failgate.settings import Settings
    from failgate.verify.related import failed_nodes, outcomes

    settings = Settings()
    sandbox = DockerSandbox.from_settings(settings)
    gh, pypi = GitHubRest(settings.github_token), PyPIClient(settings.pypi_url)
    tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi, run_timeout_s=600)
    cfg = PackageConfig(name=spec["package"], subdir=spec.get("subdir"), test_deps=deps)
    result: dict[str, Any] = {k: spec[k] for k in ("sha", "path", "function", "tests")}
    try:
        clean = await fetch_github_tree(gh, spec["repo"], spec["sha"])
        source = clean.read_files(lambda p: p == spec["path"], max_bytes=2**22)[spec["path"]]
        fn = dict(_functions(ast.parse(source)))[spec["function"]]
        bad = clean.overlay({spec["path"]: inject_raise(source, fn)}, label="inject")
        for label, tree in (("clean", clean), ("injected", bad)):
            prep = await tester.prepare(cfg, tree, number=0, python=spec.get("python"),
                                        version=spec.get("version"), pytest=spec.get("pytest"))
            ws = await tester.open_workspace(prep, f"inject-{label}")
            try:
                run = await sandbox.run(
                    prep.env.image, ws,
                    ["python", "-m", "pytest", *[f"{WORK_SRC}/{t}" for t in spec["tests"]],
                     "-q", "-rA", "-p", "no:cacheprovider", f"--rootdir={WORK_SRC}",
                     "--continue-on-collection-errors"],
                    timeout_s=1200, allowed=RUN_PREFIXES)
            finally:
                await sandbox.remove_workspace(ws)
            text = run.stdout + "\n" + run.stderr
            result[label] = {"exit": run.exit_code, "ran": len(outcomes(text)),
                             "failed": sorted(failed_nodes(text)),
                             "tail": run.output_tail(5)}
            print(f"== {label}: exit {run.exit_code}, {len(outcomes(text))} results, "
                  f"{len(failed_nodes(text))} failed", flush=True)
        new = sorted(set(result["injected"]["failed"]) - set(result["clean"]["failed"]))
        result["new_failures"] = new
        print(f"new failures caused by the injection: {len(new)}", flush=True)
        for n in new[:30]:
            print("  " + n, flush=True)
    finally:
        await gh.aclose()
        await pypi.aclose()
    await asyncio.to_thread(out.write_text, json.dumps(result, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def main() -> None:
    name = sys.argv[1]
    spec = json.loads(Path("eval/actions", f"{name}.json").read_text(encoding="utf-8"))
    from failgate.replay import verify_eval as ve

    deps = ve.load_verify_config(spec["repo"]).test_deps
    os.environ.setdefault("REPRO_BUDGET_USD", str(spec.get("budget_per_issue", 0.05)))
    Path("eval/cache").mkdir(parents=True, exist_ok=True)
    common = ["--package", spec["package"]]
    if spec.get("import_name"):
        common += ["--import-name", spec["import_name"]]
    if spec.get("subdir"):
        common += ["--subdir", spec["subdir"]]
    if deps:
        common += ["--test-deps", ",".join(deps)]
    stage = spec["stage"]
    print(f"stage={stage} test_deps={deps} REPRO_BUDGET_USD={os.environ['REPRO_BUDGET_USD']}")

    if stage in ("smoke", "inject"):
        out = Path("eval/runs") / f"{spec['repo'].replace('/', '__')}__{stage}__" \
            f"{datetime.now():%Y%m%d-%H%M}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        asyncio.run((smoke if stage == "smoke" else inject)(spec, deps, out))
        return

    print(f"LLM balance before: {balance()}", flush=True)
    try:
        if stage == "l2":
            failgate("index", "build", spec["repo"], "--limit",
                     str(spec.get("backfill_limit", 6000)), "--db", REPLAY_DB)
            failgate("replay", "l2", spec["repo"], *common, "--limit", str(spec["limit"]),
                     "--offset", str(spec["offset"]), "--since", spec.get("since", "2022-01-01"))
        elif stage == "checkup":
            failgate("checkup", spec["repo"], *common, "--accept-rule",
                     "--limit", str(spec["limit"]), "--offset", str(spec["offset"]),
                     "--since", spec.get("since", "2022-01-01"),
                     "--backfill-limit", str(spec.get("backfill_limit", 6000)))
        else:
            raise SystemExit(f"unknown stage {stage!r}")
    finally:
        print(f"LLM balance after: {balance()}", flush=True)


if __name__ == "__main__":
    main()
