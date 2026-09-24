"""分诊回放评测：拿维护者最终打上的类型标签当标准答案（技术方案第 18 节）。

标准答案：只取恰好带一个类型标签（如 psf/black 的 "T: bug"）的 issue；带多个类型标签的有歧义，
不参与评测。类型标签到分诊类型的映射由 TYPE_MAP 给出；没有对应类型的标签（例如 black 的
"T: style"，介于 bug 和功能请求之间）不参与"类型准确率"，只参与"类型标签准确率"。

时间旅行：分诊只看到 issue 的标题和正文，看不到维护者后来的评论和标签。
抽样：按标准答案分层，每类最多 per_class 个；按固定种子再分成开发集 / 留出集两半，
改提示词时只看开发集，最后报告留出集，避免"在同一批数据上调好又报告"的选择偏差。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from warden.db import Database, IssueDoc, Repo
from warden.llm import LLMClient
from warden.skills.base import IssueSnapshot, SkillContext
from warden.skills.intake import IntakeOutput, IntakeSkill
from warden.skills.triage import TriageOutput, TriageSkill

from .dataset import dataset_dir, repo_slug
from .dedup import SkillCache, _content_hash
from .metrics import wilson

# psf/black 的类型标签 → 分诊类型；None 表示没有对应类型（只评"类型标签"）
TYPE_MAP: dict[str, str | None] = {
    "T: bug": "bug",
    "T: enhancement": "feature",
    "T: documentation": "docs",
    "T: user support": "question",
    "T: style": None,
}
TYPE_PREFIX = "T: "

Split = Literal["dev", "holdout"]


class RepoLabel(BaseModel):
    name: str
    description: str = ""


class TriageRecord(BaseModel):
    issue: int
    year: int
    split: Split
    gold_label: str
    gold_type: str | None
    title: str = ""
    pred_type: str | None = None
    pred_labels: list[str] = Field(default_factory=list)
    confidence: float | None = None
    slop_score: float | None = None
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None

    @property
    def judged(self) -> bool:
        return self.pred_type is not None

    @property
    def pred_type_labels(self) -> list[str]:
        return [x for x in self.pred_labels if x.startswith(TYPE_PREFIX)]


class TriageRunConfig(BaseModel):
    repo: str
    per_class: int = 40
    seed: int = 42
    prompt_version: str = "1"
    model: str = "deepseek-flash"
    concurrency: int = 6


class TriageRunResult(BaseModel):
    config: TriageRunConfig
    started_at: datetime
    finished_at: datetime | None = None
    corpus_size: int
    # 每类在语料里的真实数量（只算单一类型标签的 issue），用于按真实分布加权
    population: dict[str, int]
    labels_fingerprint: str
    records: list[TriageRecord] = Field(default_factory=list)
    cost_usd: float = 0.0
    model_calls: int = 0
    cached_calls: int = 0


def gold_label(labels: Sequence[str]) -> str | None:
    """恰好一个已知类型标签时返回它；没有或多于一个返回 None。"""
    found = [x for x in labels if x in TYPE_MAP]
    types = [x for x in labels if x.startswith(TYPE_PREFIX)]
    return found[0] if len(found) == 1 and len(types) == 1 else None


def sample(
    docs: Sequence[IssueDoc], *, per_class: int, seed: int
) -> list[tuple[IssueDoc, str, Split]]:
    """分层抽样 + 开发集/留出集划分。每类内部先按编号排序再打乱，保证可复现。"""
    by_label: dict[str, list[IssueDoc]] = {}
    for d in docs:
        g = gold_label(d.labels or [])
        if g is not None:
            by_label.setdefault(g, []).append(d)
    rng = random.Random(seed)
    out: list[tuple[IssueDoc, str, Split]] = []
    for label in sorted(by_label):
        pool = sorted(by_label[label], key=lambda d: d.number)
        rng.shuffle(pool)
        for i, d in enumerate(pool[:per_class]):
            out.append((d, label, "dev" if i % 2 == 0 else "holdout"))
    return out


def labels_fingerprint(labels: Sequence[RepoLabel]) -> str:
    raw = json.dumps([lb.model_dump() for lb in labels], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def labels_path(repo: str, root: Path) -> Path:
    return dataset_dir(repo, root) / "repo_labels.json"


def load_repo_labels(repo: str, root: Path) -> list[RepoLabel] | None:
    p = labels_path(repo, root)
    if not p.exists():
        return None
    return [RepoLabel.model_validate(x) for x in json.loads(p.read_text("utf-8"))]


def save_repo_labels(repo: str, labels: Sequence[RepoLabel], root: Path) -> Path:
    p = labels_path(repo, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [lb.model_dump() for lb in sorted(labels, key=lambda x: x.name)]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    return p


async def run_triage_replay(
    db: Database,
    llm: LLMClient,
    cfg: TriageRunConfig,
    repo_labels: Sequence[RepoLabel],
    *,
    cache_root: Path,
    progress: Callable[[str], None] = print,
) -> TriageRunResult:
    async with db.session() as s:
        repo = await s.scalar(select(Repo).where(Repo.full_name == cfg.repo))
        if repo is None:
            raise RuntimeError(f"数据库里没有 {cfg.repo}，请先运行 warden index build")
        docs = list((await s.scalars(select(IssueDoc).where(IssueDoc.repo_id == repo.id))).all())

    population = Counter(g for d in docs if (g := gold_label(d.labels or [])) is not None)
    fp = labels_fingerprint(repo_labels)
    result = TriageRunResult(
        config=cfg,
        started_at=datetime.now(UTC),
        corpus_size=len(docs),
        population=dict(population),
        labels_fingerprint=fp,
    )
    picked = sample(docs, per_class=cfg.per_class, seed=cfg.seed)
    progress(f"抽样 {len(picked)} 个（每类最多 {cfg.per_class} 个）：{dict(population)}")

    cache = SkillCache(cache_root / repo_slug(cfg.repo))
    intake_skill, triage_skill = IntakeSkill(), TriageSkill()
    triage_skill.version = cfg.prompt_version
    names = tuple(lb.name for lb in repo_labels)
    descriptions = {lb.name: lb.description for lb in repo_labels}
    sem = asyncio.Semaphore(cfg.concurrency)
    records: list[TriageRecord] = []
    done = 0

    async def judge(d: IssueDoc, label: str, split: Split) -> None:
        nonlocal done
        rec = TriageRecord(
            issue=d.number, year=d.created_at.year, split=split, gold_label=label,
            gold_type=TYPE_MAP[label], title=d.title[:100],
        )
        records.append(rec)
        ctx = SkillContext(
            issue=IssueSnapshot(repo=cfg.repo, number=d.number, title=d.title, body=d.body,
                                repo_id=repo.id, created_at=d.created_at),
            llm=llm, model=cfg.model, labels=names, label_descriptions=descriptions,
        )
        async with sem:
            try:
                # Intake 的缓存键和查重回放一致，两边可以共用
                ikey = ("intake", intake_skill.version, cfg.model, str(d.number), _content_hash(d))
                hit = cache.get(*ikey)
                if hit is None:
                    r = await intake_skill.run(ctx)
                    hit = {"output": r.output.model_dump(mode="json"), "cost": r.cost_usd}
                    cache.put(hit, *ikey)
                    result.model_calls += 1
                    rec.cost_usd += r.cost_usd
                else:
                    result.cached_calls += 1
                ctx.prior["intake"] = IntakeOutput.model_validate(hit["output"]).model_dump(
                    mode="json"
                )
                tkey = ("triage", triage_skill.version, cfg.model, str(d.number),
                        _content_hash(d), fp)
                hit = cache.get(*tkey)
                if hit is None:
                    r = await triage_skill.run(ctx)
                    hit = {"output": r.output.model_dump(mode="json"), "cost": r.cost_usd}
                    cache.put(hit, *tkey)
                    result.model_calls += 1
                    rec.cost_usd += r.cost_usd
                else:
                    result.cached_calls += 1
                    rec.cached = True
                out = TriageOutput.model_validate(hit["output"])
                rec.pred_type, rec.pred_labels = out.type, out.labels
                rec.confidence, rec.slop_score = out.confidence, out.slop_score
            except Exception as e:  # 单个样本失败不影响整轮评测
                rec.error = f"{type(e).__name__}: {e}"[:300]
            done += 1
            if done % 20 == 0 or done == len(picked):
                progress(f"  进度 {done}/{len(picked)}")

    await asyncio.gather(*(judge(d, lb, sp) for d, lb, sp in picked))
    result.records = sorted(records, key=lambda r: (r.gold_label, r.issue))
    result.cost_usd = round(sum(r.cost_usd for r in records), 4)
    result.finished_at = datetime.now(UTC)
    return result


# ---------------- 指标 ----------------


@dataclass
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float:
        return self.k / self.n if self.n else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson(self.k, self.n)


@dataclass
class TriageMetrics:
    split: str
    judged: int
    errors: int
    # 类型准确率：只算有对应类型的样本
    type_acc: Rate
    per_class: dict[str, Rate]
    # 按类平均（每类同等重要）与按语料真实分布加权（更接近线上看到的准确率）
    macro: float
    weighted: float
    # 类型标签：预测的 T: 标签恰好等于标准答案
    label_exact: Rate
    label_per_class: dict[str, Rate]
    # 混淆矩阵：标准答案标签 → 预测类型 → 数量
    confusion: dict[str, dict[str, int]]
    # 置信度分桶：桶 → 类型准确率
    calibration: dict[str, Rate]
    by_period: dict[str, Rate]
    extra: dict[str, float] = field(default_factory=dict)


CONF_BUCKETS = ((0.0, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.01))


def evaluate_triage(
    records: Sequence[TriageRecord], population: dict[str, int], split: str = "all"
) -> TriageMetrics:
    rows = [r for r in records if split == "all" or r.split == split]
    judged = [r for r in rows if r.judged]
    typed = [r for r in judged if r.gold_type is not None]

    def rate(rs: Sequence[TriageRecord]) -> Rate:
        return Rate(sum(1 for r in rs if r.pred_type == r.gold_type), len(rs))

    per_class = {
        lb: rate([r for r in typed if r.gold_label == lb])
        for lb in sorted({r.gold_label for r in typed})
    }
    macro = sum(x.value for x in per_class.values()) / len(per_class) if per_class else 0.0
    pop = {lb: population.get(lb, 0) for lb in per_class}
    total = sum(pop.values())
    weighted = sum(per_class[lb].value * pop[lb] / total for lb in per_class) if total else 0.0

    def label_hit(r: TriageRecord) -> bool:
        return r.pred_type_labels == [r.gold_label]

    label_per_class = {
        lb: Rate(sum(1 for r in judged if r.gold_label == lb and label_hit(r)),
                 sum(1 for r in judged if r.gold_label == lb))
        for lb in sorted({r.gold_label for r in judged})
    }
    confusion: dict[str, dict[str, int]] = {}
    for r in judged:
        row = confusion.setdefault(r.gold_label, {})
        row[r.pred_type or "?"] = row.get(r.pred_type or "?", 0) + 1

    calibration: dict[str, Rate] = {}
    for lo, hi in CONF_BUCKETS:
        bucket = [r for r in typed if r.confidence is not None and lo <= r.confidence < hi]
        calibration[f"[{lo:.2f}, {min(hi, 1.0):.2f}{']' if hi > 1 else ')'}"] = rate(bucket)

    by_period = {
        "≤2023": rate([r for r in typed if r.year <= 2023]),
        "≥2024": rate([r for r in typed if r.year >= 2024]),
    }
    return TriageMetrics(
        split=split,
        judged=len(judged),
        errors=sum(1 for r in rows if r.error),
        type_acc=rate(typed),
        per_class=per_class,
        macro=macro,
        weighted=weighted,
        label_exact=Rate(sum(1 for r in judged if label_hit(r)), len(judged)),
        label_per_class=label_per_class,
        confusion=confusion,
        calibration=calibration,
        by_period=by_period,
        extra={
            "slop_flagged": sum(1 for r in judged if (r.slop_score or 0) >= 0.5),
            "cost_usd": round(sum(r.cost_usd for r in rows), 4),
        },
    )


# ---------------- 报告 ----------------


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _rate(r: Rate) -> str:
    lo, hi = r.ci
    return f"{r.k}/{r.n} = {_pct(r.value)} [{lo * 100:.0f}%, {hi * 100:.0f}%]" if r.n else "—"


def render_triage_report(run: TriageRunResult) -> str:
    cfg = run.config
    splits = [evaluate_triage(run.records, run.population, s) for s in ("dev", "holdout", "all")]
    holdout = splits[1]
    lines = [
        f"# 分诊回放评测：{cfg.repo} · {run.started_at:%Y-%m-%d %H:%M} UTC · "
        f"triage_v{cfg.prompt_version}",
        "",
        f"- 模型 `{cfg.model}`；每类最多 {cfg.per_class} 个，种子 {cfg.seed}；"
        f"标签表指纹 `{run.labels_fingerprint}`；语料 {run.corpus_size} 个 issue",
        f"- 标准答案：维护者最终打上的**唯一**类型标签（{', '.join(TYPE_MAP)}）；"
        "分诊只看标题和正文（时间旅行）",
        f"- 语料中单一类型标签的分布：{json.dumps(run.population, ensure_ascii=False)}",
        f"- 模型调用 {run.model_calls} 次（缓存命中 {run.cached_calls}），花费 ${run.cost_usd:.4f}",
        "",
        "## 结论（留出集）",
        "",
        f"- **类型准确率** {_rate(holdout.type_acc)}（按类平均 {_pct(holdout.macro)}，"
        f"按语料真实分布加权 {_pct(holdout.weighted)}；目标 ≥ 85%）",
        f"- **类型标签准确率** {_rate(holdout.label_exact)}（预测的 `T:` 标签恰好等于标准答案，"
        "含没有对应类型的 `T: style`）",
        "",
        "## 各集合对比",
        "",
        "| 集合 | 样本 | 类型准确率 | 按类平均 | 加权 | 类型标签准确率 | 出错 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in splits:
        lines.append(
            f"| {m.split} | {m.judged} | {_rate(m.type_acc)} | {_pct(m.macro)} | "
            f"{_pct(m.weighted)} | {_rate(m.label_exact)} | {m.errors} |"
        )
    m = splits[2]
    lines += ["", "## 每类（全部样本）", "",
              "| 标准答案 | 对应类型 | 类型准确率 | 类型标签准确率 |", "|---|---|---|---|"]
    for lb in sorted(m.label_per_class):
        pc = m.per_class.get(lb)
        lines.append(
            f"| `{lb}` | {TYPE_MAP.get(lb) or '（无）'} | {_rate(pc) if pc else '—'} | "
            f"{_rate(m.label_per_class[lb])} |"
        )
    types = ["bug", "feature", "question", "docs", "other"]
    lines += ["", "## 混淆矩阵（全部样本：行 = 标准答案，列 = 预测类型）", "",
              "| 标准答案 | " + " | ".join(types) + " |", "|---" * (len(types) + 1) + "|"]
    for lb in sorted(m.confusion):
        row = m.confusion[lb]
        lines.append(f"| `{lb}` | " + " | ".join(str(row.get(t, 0)) for t in types) + " |")
    lines += ["", "## 置信度校准（全部样本，只算有对应类型的）", "",
              "| 置信度 | 类型准确率 |", "|---|---|"]
    lines += [f"| {b} | {_rate(r)} |" for b, r in m.calibration.items()]
    lines += ["", "## 时间段（检查训练数据污染：越新的 issue 越不可能在训练数据里）", "",
              "| 创建时间 | 类型准确率 |", "|---|---|"]
    lines += [f"| {p} | {_rate(r)} |" for p, r in m.by_period.items()]
    wrong = [r for r in run.records if r.judged and r.gold_type and r.pred_type != r.gold_type]
    lines += ["", f"## 判错的样本（{len(wrong)} 个）", "",
              "| issue | 集合 | 标准答案 | 预测 | 置信度 | 标题 |", "|---|---|---|---|---|---|"]
    for r in sorted(wrong, key=lambda r: (r.gold_label, r.issue)):
        title = r.title.replace("|", "\|")
        lines.append(
            f"| [#{r.issue}](https://github.com/{cfg.repo}/issues/{r.issue}) | {r.split} | "
            f"`{r.gold_label}` | {r.pred_type} | {r.confidence:.2f} | {title} |"
        )
    lines += [
        "",
        "## 局限",
        "",
        "- 标准答案只有维护者的类型标签；维护者没打类型标签的 issue 不在评测范围内，"
        "分诊给出 `other` 一律算错。",
        "- 正文是现在的版本：如果作者后来编辑过（例如补充了信息），分诊看到的比当时多。",
        "- 老 issue 可能在模型的训练数据里，见上面的时间段对比。",
        "",
    ]
    return "\n".join(lines)
