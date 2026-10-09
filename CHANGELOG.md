# Changelog

All notable changes to FailGate are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: anything may still change).

## [0.1.0] - 2026-10-09

First public release. FailGate is the acceptance layer for bug fixes: when a bug is
reported it writes a failing test in the repository's own test suite, seals it, and later
verifies any fix — from a maintainer, a contributor or an AI agent — against that sealed test.

### Added

- **Exam writing (L2).** An agent reproduces a reported bug as a pytest test inside the
  repository, at the commit the issue was filed against, in a network-isolated Docker
  sandbox. A test only counts when it fails the way the issue describes.
- **Sealing and receipts.** The test is hashed together with its environment and run
  results into a receipt (`failgate.receipt/v1`) that anyone can re-hash; maintainers can
  replace it explicitly with `/failgate reseal`.
- **Three-layer verification (ClaimVerify).** For a PR that claims to fix an issue:
  ① the sealed test fails on the merge base and passes on the PR, ② the PR did not tamper
  with the test, its skips or the pytest configuration, ③ related existing tests show no
  new failures.
- **Exam strength and hidden exams.** Mutation testing on the lines a fix changed scores
  how much the sealed test actually checks; optional hidden variants catch fixes that only
  satisfy the public test. Both are shown as hints and never flip a verdict.
- **Fixer agent.** A LangGraph agent that may edit source but not tests or test
  configuration; its patch goes through the same verification. Triggered with
  `/failgate fix`; pushes go through a separate GitHub App that can only push
  `failgate/fix-N` branches.
- **Ways to run it.** GitHub App (issues and PRs), CLI (`failgate verify owner/repo#N`,
  `failgate checkup`, interactive shell), local MCP server for coding agents, read-only web
  console, and `docker compose` deployment.
- **Monorepo support.** Packages that live in a subdirectory (`--subdir libs/core`).
- **Repository checkup.** `failgate checkup owner/repo` runs the whole evaluation on a
  repository's own closed bugs and reports the numbers.

### Measured (small samples, details in the README)

- Strict fail-before / pass-after on upstream fixes: 32/36 across 4 Python repositories,
  plus LangChain `libs/core` 4/5 on a held-out set.
- Verdicts on real fixes and four kinds of cheating PRs: 125/125 (3 repositories), and
  22/23 on LangChain including injected regressions.

### Known limitations

- Python + pytest only. Packages that need system libraries may not install in the sandbox.
- "Verified" means *passes the sealed test, no tampering found, no new regressions* — not
  "the fix is correct". A weak test lets a wrong fix through.
- Exam strength cannot analyse files that use `match` statements yet (reported as n/a).
- Runtime dependencies are not time-travelled: old commits install today's versions of
  their dependencies.
- Installing pulls in a fairly heavy dependency set (LangGraph, OpenTelemetry, MCP,
  cosmic-ray); splitting it into extras is planned for a later release.
- The sandbox needs Docker; the run phase has no network, but the install phase runs the
  project's build backend (behind an egress allow-list).
