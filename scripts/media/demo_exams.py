"""README 素材用：把演示仓库已公开的考卷导出成 JSON，或导入一个空库。

考卷代码和收据本来就贴在 failgate-demo 的 issue 评论里，导出的只是这些公开内容；
隐藏考卷（hidden_exams）不导出。GitHub Actions 录命令行动图时用导入的库跑真实的
`failgate verify`。

    python scripts/media/demo_exams.py export --db <live url> --out docs/media/demo-exams.json
    python scripts/media/demo_exams.py import            # 导入本机配置的库（FAILGATE_DB_URL）
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from failgate.db import Case, Database, Evidence, Repo

REPO = "san086041-glitch/failgate-demo"
ISSUES = (1, 2, 3)
EVIDENCE_COLS = ("id", "level", "mode", "acceptance", "test_path", "test_code", "test_sha256",
                 "source_repo", "source_sha", "python", "pytest", "verdict", "fail_rate",
                 "receipt", "receipt_sha256")


async def export(db_url: str, out: Path) -> None:
    db = Database(db_url)
    try:
        async with db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.full_name == REPO))).scalar_one()
            rows = (await s.execute(
                select(Evidence, Case.number).join(Case, Evidence.case_id == Case.id)
                .where(Case.repo_id == repo.id, Case.number.in_(ISSUES),
                       Evidence.acceptance.is_(True), Evidence.superseded_by.is_(None))
                .order_by(Case.number, Evidence.created_at)
            )).all()
            data = {
                "repo": REPO,
                "import_name": repo.repro_import_name,
                "exams": [{"issue": n, **{c: getattr(ev, c) for c in EVIDENCE_COLS}}
                          for ev, n in rows],
            }
    finally:
        await db.dispose()
    out.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")  # noqa: ASYNC240
    print(f"{len(data['exams'])} exams -> {out}")


async def load(db_url: str, src: Path) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))  # noqa: ASYNC240
    db = Database(db_url)
    await db.create_all()
    try:
        async with db.session() as s, s.begin():
            if (await s.execute(select(Repo).where(Repo.full_name == data["repo"]))).first():
                print(f"{data['repo']} is already in {db_url}; nothing imported")
                return
            repo = Repo(full_name=data["repo"], platform="github",
                        repro_import_name=data["import_name"])
            s.add(repo)
            await s.flush()
            for ex in data["exams"]:
                case = Case(repo_id=repo.id, number=ex["issue"], kind="issue", state="REPRODUCED")
                s.add(case)
                await s.flush()
                s.add(Evidence(case_id=case.id, **{c: ex[c] for c in EVIDENCE_COLS}))
    finally:
        await db.dispose()
    print(f"imported {len(data['exams'])} exams into {db_url}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("--db", help="默认读配置（FAILGATE_DB_URL）")
    p.add_argument("--out", type=Path, default=Path("docs/media/demo-exams.json"))
    p.add_argument("--src", type=Path, default=Path("docs/media/demo-exams.json"))
    a = p.parse_args()
    if a.db is None:
        from failgate.settings import Settings

        a.db = Settings().failgate_db_url
    asyncio.run(export(a.db, a.out) if a.action == "export" else load(a.db, a.src))


if __name__ == "__main__":
    main()
