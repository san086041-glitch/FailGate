# 修复评测集 v2：psf/black

- 规则：2022-01-01 之后创建、已完成关闭的 `T: bug`，不带排除标签，按编号从新到旧；上游修复要有源码和测试改动、不改依赖，金标准 F2P 非空；凑够 36 题为止
- 排除（之前的开发集 / 留出集）：24 个
- 收下 36 题，跳过 64 个；开始于 2026-10-01T18:51:14

## 收下的题

| issue | 创建 | 修复 PR | Python | F2P | 上游改的源码 | 标题 |
|---|---|---|---|---|---|---|
| #5307 | 2026-08-12 | #5311 | 3.14 | 1 | src/black/comments.py | Crash on `async def`/`with` and a semicolon on the same line |
| #5285 | 2026-07-31 | #5312 | 3.14 | 1 | src/black/lines.py | Empty new line added after docstring when using `--line-rang |
| #5225 | 2026-07-06 | #5238 | 3.14 | 1 | src/black/lines.py, src/black/mode.py, src/black/resources/black.schema.json | Inconsistent number of lines after import |
| #5187 | 2026-06-18 | #5189 | 3.14 | 1 | src/black/comments.py, src/black/linegen.py | INTERNAL ERROR crash; syntax error in log |
| #5164 | 2026-06-03 | #5167 | 3.14 | 1 | src/black/__init__.py | Unexpected version warning |
| #5138 | 2026-05-18 | #5139 | 3.14 | 1 | src/black/comments.py | INTERNAL ERROR on 26.5.0 |
| #5122 | 2026-05-04 | #5117 | 3.14 | 1 | src/black/comments.py | Fail to parse multiline `case` statement with `# fmt: skip` |
| #5112 | 2026-04-21 | #5117 | 3.14 | 1 | src/black/comments.py | `# fmt: skip` regression |
| #4950 | 2026-01-10 | #4952 | 3.14 | 1 | src/black/concurrency.py | Manager not properly shut down in concurrency module - viola |
| #4783 | 2025-10-06 | #4800 | 3.14 | 1 | src/black/comments.py | Preview style `fix_fmt_skip_in_one_liners` does not work for |
| #4762 | 2025-09-21 | #4764 | 3.13 | 1 | src/black/lines.py, src/black/mode.py, src/black/resources/black.schema.json, sr | module_docstring_newlines inconsistent w/ first line comment |
| #4740 | 2025-08-30 | #4777 | 3.13 | 1 | src/black/linegen.py, src/black/mode.py, src/black/resources/black.schema.json | Bug: fails to properly format constrained generics with new  |
| #4733 | 2025-08-14 | #5130 | 3.14 | 1 | src/black/linegen.py | Internal error: producing different code on second pass of t |
| #4730 | 2025-08-07 | #4903 | 3.14 | 1 | src/black/comments.py | `# fmt: skip` ignored inside multi-part if-clause |
| #4669 | 2025-05-28 | #4670 | 3.13 | 1 | src/black/ranges.py | Parser fails due to empty body `...` on same line as definit |
| #4653 | 2025-04-22 | #4884 | 3.14 | 1 | src/black/linegen.py, src/black/mode.py, src/black/resources/black.schema.json | Trailing comma in `case` pattern causes `if` guard to explod |
| #4647 | 2025-04-09 | #4684 | 3.13 | 1 | src/black/linegen.py | Internal error in the formatting of the if expression within |
| #4632 | 2025-03-20 | #4634 | 3.13 | 1 | src/black/linegen.py, src/black/nodes.py | Parsing the with statement with tuple argument failed |
| #4629 | 2025-03-20 | #4630 | 3.13 | 1 | src/black/linegen.py | failed to format a file with a with-statement and a named-ex |
| #4625 | 2025-03-17 | #4628 | 3.13 | 1 | src/black/parsing.py | crash while formatting a delete statement with given line le |
| #4535 | 2024-12-21 | #4552 | 3.13 | 1 | src/black/comments.py, src/black/mode.py, src/black/resources/black.schema.json | `# fmt: skip` is not being respected with one-liner function |
| #4511 | 2024-11-14 | #5097 | 3.14 | 1 | src/black/linegen.py | Error formatting f-string with `# fmt: off` inside a pair of |
| #4510 | 2024-11-11 | #5096 | 3.14 | 1 | src/black/trans.py | string_processing: Multi-line strings with type ignore pragm |
| #4465 | 2024-09-27 | #4466 | 3.12 | 1 | src/black/mode.py | Cache causes confusing behavior when running with and withou |
| #4430 | 2024-08-06 | #5175 | 3.14 | 1 | src/black/__init__.py, src/black/comments.py, src/black/linegen.py, src/black/li | `--line-ranges` formats lines outside of range |
| #4421 | 2024-07-31 | #4422 | 3.12 | 1 | src/blib2to3/pgen2/tokenize.py | Crash on f-string with `\{` |
| #4366 | 2024-05-17 | #4855 | 3.14 | 2 | src/black/comments.py | fmt: skip is required at the line which follows the one inco |
| #4350 | 2024-05-05 | #4401 | 3.12 | 1 | src/black/linegen.py | "EOF in multi-line string" on string containing same quote n |
| #4349 | 2024-05-05 | #5095 | 3.14 | 1 | src/black/linegen.py, src/black/lines.py, src/black/mode.py, src/black/resources | Unnecessary parentheses added to expression in indexed assig |
| #4337 | 2024-04-26 | #4339 | 3.12 | 1 | src/blib2to3/pgen2/tokenize.py | Cannot parse multiline f-string containing multiline string |
| #4329 | 2024-04-24 | #4332 | 3.12 | 1 | src/blib2to3/pgen2/tokenize.py | Black cannot parse previously parseable file in 24.4.1 |
| #4324 | 2024-04-22 | #4325 | 3.12 | 1 | src/black/linegen.py, src/black/nodes.py | PEP 701 support breaks stability policy |
| #4288 | 2024-03-22 | #4290 | 3.12 | 1 | src/black/parsing.py | When reformatting a triple-quote string, black fails with an |
| #4268 | 2024-03-08 | #4270 | 3.12 | 1 | src/black/__init__.py, src/black/parsing.py | AST safety check fails to catch incorrect f-string change |
| #4264 | 2024-03-04 | #4273 | 3.12 | 1 | src/black/__init__.py, src/black/ranges.py | `--line-ranges` formats entire file when ranges are at EOF |
| #4256 | 2024-02-28 | #5092 | 3.14 | 1 | src/black/lines.py, src/black/mode.py, src/black/resources/black.schema.json | Black removes blank lines in between a function and a decora |

## 跳过的原因

| 原因 | 个数 |
|---|---|
| no_fix_commit | 37 |
| no_test_change | 10 |
| gold_no_f2p | 6 |
| no_source_change | 5 |
| deps_changed | 2 |
| over_target | 2 |
| gold_infra | 1 |
| env | 1 |

## 按年份

2024：16、2025：11、2026：9
