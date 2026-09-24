# 回放评测

用仓库自己的历史当标准答案，离线评估 RepoWarden 的能力模块。目前支持查重。

## 目录

| 路径 | 内容 | 是否提交 |
|---|---|---|
| `datasets/<owner>__<name>/dedup_gold.json` | 维护者确认的重复对（从 "Duplicate of #N" 评论挖掘） | ✅ |
| `datasets/<owner>__<name>/labels.json` | 人工复核结论：对照样本被判为重复时，到底是不是 | ✅ |
| `runs/*.json` | 每次评测的记录：只有编号、分数、模型判断，不含 issue 正文 | ✅ |
| `reports/*.md` | 评测报告 | ✅ |
| `cache/` | 语料数据库、模型输出缓存 | ❌ |

issue 正文不提交。报告里记录了语料指纹，用 `warden index build` 重建语料后可以核对。

## 流程

```bash
# 1. 回填语料到独立的评测数据库（不碰线上数据）
warden index build psf/black --limit 5000 --db sqlite+aiosqlite:///eval/cache/replay.db

# 2. 挖掘标准答案（只认 OWNER / MEMBER / COLLABORATOR 的 "Duplicate of #N"）
warden replay mine psf/black

# 3. 只做召回评测（不调用模型，几秒钟）
warden replay dedup psf/black --no-judge

# 4. 完整评测：召回 + 抽样判断（调用模型）+ 阈值扫描
warden replay dedup psf/black --positives 100 --negatives 50 --seed 42

# 5. 复核报告里"待人工复核"的配对，写进 labels.json，然后离线重算（不调用模型）
warden replay sweep eval/runs/<run>.json
```

GitHub 的限额：设置 `GITHUB_TOKEN`（可以用 `gh auth token`）。

## 指标怎么算

- **正样本**：维护者确认的重复。判为 duplicate 且目标在同一重复簇里算判对；目标不对算指错；否则算漏判。
- **对照样本**：不在任何重复簇里、没被标成 duplicate、之前至少有 50 个 issue 的普通 issue。很多重复从未被标注，所以被判为重复时看人工复核：确认是重复的算对，其余（不是、模棱两可、未复核）保守地算错。
- **精确率** = 判对的 duplicate 结论 / 全部 duplicate 结论。**召回率** = 判对的正样本 / 正样本数（包括召回阶段就漏掉的）。都附 Wilson 95% 置信区间。
- **阈值扫描**：评测记录里存的是模型的原始判断，等级由 `warden.skills.dedup.finalize` 按阈值重算，和线上用的是同一个函数，所以换阈值不需要重新调用模型。
