"""考卷强度的外部校准（ADR 0022）：强度分能不能发现"测试不够"？

UTBoost 找出了 SWE-bench 里 36 个"官方测试不够、放过了错补丁"的题（和 Verified 重合 26 个），
并给出了增强测试。如果强度分有用，那么对同一个标准修复、同一批变异体：

    官方考卷（官方 FAIL_TO_PASS）的杀死率  <  UTBoost 考卷（加了增强测试）的杀死率

另外抽 26 个 UTBoost 没发现问题的题当对照组，看"测试不够"的题官方考卷的强度是否偏低。

每个题在 Epoch AI 预建的 SWE-bench 镜像里跑（代码在 /testbed，conda 环境 testbed）。
测试命令、测试目录、日志解析都用 swebench 官方库（`pip install swebench==4.1.0`，只在
GitHub Actions 里装），不自己写。变异体生成复用 strength.py（cosmic-ray + 自带算子）。

在 GitHub Actions 上运行（.github/workflows/strength-calibration.yml）：每个题一个任务，
镜像在云端机器上下载，本机没有任何下载。
"""

from __future__ import annotations

import json
import math
import random
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from failgate.verify.strength import changed_lines, expand_executed, mutants_for_file, sample

from .metrics import wilson

IMAGE = "ghcr.io/epoch-research/swe-bench.eval.x86_64.{iid}:latest"
MAX_MUTANTS = 30
MIN_TIMEOUT_S = 60
TIMEOUT_FACTOR = 3
PASSING = ("PASSED", "XFAIL")  # 和 swebench grading 一样：这两种算通过
_DJANGO = re.compile(r"^(\w+) \(([\w.]+)\)")
_TEST_DIRS = ("tests/", "test/", "testing/")


@dataclass
class Condition:
    """一种考卷：用哪个 test_patch，考卷由哪些 FAIL_TO_PASS 测试组成。"""

    name: Literal["official", "utboost"]
    test_patch: str
    fail_to_pass: list[str]


@dataclass
class Instance:
    instance_id: str
    repo: str
    version: str
    base_commit: str
    patch: str  # 标准修复（gold patch）
    group: Literal["utboost", "control"]
    conditions: list[Condition]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, row: dict[str, Any], group: Literal["utboost", "control"]) -> Instance:
        conds = [Condition("official", row["test_patch"], json.loads(row["FAIL_TO_PASS"]))]
        if group == "utboost":
            conds.append(Condition("utboost", row["utboost_test_patch"],
                                   json.loads(row["utboost_FAIL_TO_PASS"])))
        return cls(instance_id=row["instance_id"], repo=row["repo"], version=row["version"],
                   base_commit=row["base_commit"], patch=row["patch"], group=group,
                   conditions=conds, raw=row)


def load_instances(path: Path) -> list[Instance]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ([Instance.from_json(r, "utboost") for r in data["utboost"]]
            + [Instance.from_json(r, "control") for r in data["control"]])


# ---------------------------------------------------------------- 纯函数：补丁、选择器、命令


def patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)", patch, re.M)


def source_files(patch: str) -> list[str]:
    """标准修复里改动的源码文件（.py，不是测试）。"""
    out = []
    for p in patch_files(patch):
        name = p.rsplit("/", 1)[-1]
        if (p.endswith(".py") and not name.startswith("test_") and not name.endswith("_test.py")
                and name != "conftest.py" and not p.startswith(_TEST_DIRS)
                and "/tests/" not in p):
            out.append(p)
    return out


def exam_selectors(repo: str, f2p: list[str], directives: list[str]) -> list[str]:
    """只跑考卷里的测试，而不是整个测试文件（matplotlib 的 test_axes.py 要跑好几分钟）。

    - pytest 系：FAIL_TO_PASS 就是节点 ID，直接传；
    - Django：`test_x (module.Class)` → runtests 的标签 `module.Class.test_x`；
      解析不了就退回整个模块；
    - sympy：bin/test 按文件跑（swebench 的测试目录），结果再按函数名挑。"""
    if repo == "django/django":
        labels = []
        for t in f2p:
            m = _DJANGO.match(t)
            if m is None:
                return directives
            name, where = m.group(1), m.group(2)
            # Django 4.x 起输出 `test_x (module.Class.test_x)`，括号里已经带了方法名
            labels.append(where if where.endswith(f".{name}") else f"{where}.{name}")
        return labels
    if repo == "sympy/sympy":
        return directives
    return list(f2p)


def build_test_command(test_cmd: str | list[str], selectors: Sequence[str]) -> str:
    cmd = test_cmd[-1] if isinstance(test_cmd, list) else test_cmd
    return " ".join([cmd, *(shlex.quote(s) for s in selectors)])


def coverage_command(cmd: str, include: Sequence[str]) -> str | None:
    """把测试命令包进 coverage run。支持 `pytest …`、脚本（./tests/runtests.py、bin/test）和
    前面带环境变量的写法；tox 这类会另起子进程的返回 None（不取覆盖，所有改动行都算目标）。"""
    tokens = shlex.split(cmd)
    env: list[str] = []
    while tokens and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", tokens[0]):
        env.append(tokens.pop(0))
    if not tokens:
        return None
    prog, rest = tokens[0], tokens[1:]
    cov = ["python", "-m", "coverage", "run", f"--include={','.join(include)}"]
    if prog == "pytest":
        argv = [*cov, "-m", "pytest", *rest]
    elif prog.endswith(".py") or prog.startswith(("./", "bin/")):
        if prog == "bin/test" and "--no-subprocess" not in rest:
            # sympy 的测试默认在子进程里跑（为了哈希随机化），coverage 看不到子进程
            rest = ["--no-subprocess", *rest]
        # 直接运行脚本时 Python 会把脚本所在目录放进 sys.path（runtests.py 要 import
        # test_sqlite，bin/test 要 import get_sympy），经 coverage run 运行时不一定会，手动补上
        folder = prog.rsplit("/", 1)[0] if "/" in prog else "."
        env.append(f"PYTHONPATH={folder}${{PYTHONPATH:+:$PYTHONPATH}}")
        argv = [*cov, prog, *rest]
    else:
        return None
    # 环境变量原样写（里面的 ${PYTHONPATH:+…} 要交给 shell 展开），其余参数加引号
    return " ".join([*(e if e.startswith("PYTHONPATH=") else shlex.quote(e) for e in env),
                     *(shlex.quote(a) for a in argv)])


_DJANGO_HEAD = re.compile(r"^(test\w*) \(([\w.]+)\)$")
_DJANGO_RESULT = re.compile(r" \.\.\. +(ok|OK|FAIL|ERROR|skipped.*)$")


def django_docstring_status(log: str) -> dict[str, str]:
    """Django 的 --verbosity 2 在测试有文档字符串时分两行输出：

        test_x (module.Class)
        文档字符串第一行 ... ok

    swebench 的解析器把"文档字符串"当成测试名；UTBoost 的 FAIL_TO_PASS 写的却是方法名，
    两边对不上（第一轮全量运行里 4 个 Django 题因此被误判为基线失败）。这里补上方法名的结果。"""
    out: dict[str, str] = {}
    pending: str | None = None
    for raw in log.splitlines():
        line = raw.strip()
        head = _DJANGO_HEAD.match(line)
        if head:
            pending = line
            continue
        res = _DJANGO_RESULT.search(line)
        if pending and res:
            word = res.group(1)
            out[pending] = ("PASSED" if word in ("ok", "OK") else
                            "SKIPPED" if word.startswith("skipped") else "FAILED")
        pending = None
    return out


def exam_outcome(status: dict[str, str], f2p: list[str]) -> Literal["pass", "fail"]:
    """考卷里每个测试都通过才算通过；有一个没通过、或者根本没出现在日志里，都算失败。"""
    return "pass" if all(status.get(t) in PASSING for t in f2p) else "fail"


# ---------------------------------------------------------------- 容器


class Container:
    """一个 SWE-bench 实例镜像的容器。所有命令在 conda 环境 testbed、目录 /testbed 下执行。"""

    def __init__(self, image: str, *, docker: str = "docker", prelude: Sequence[str] = ()) -> None:
        self.image = image
        self.docker = docker
        self.name = f"failgate-calib-{uuid.uuid4().hex[:10]}"
        self.prelude = ["source /opt/miniconda3/bin/activate", "conda activate testbed",
                        "cd /testbed", "export PYTHONDONTWRITEBYTECODE=1",
                        "export COVERAGE_FILE=/tmp/.coverage", *prelude]

    def start(self) -> None:
        subprocess.run([self.docker, "run", "-d", "--name", self.name, self.image,
                        "sleep", "infinity"], check=True, capture_output=True)

    def offline(self) -> None:
        """装完 coverage 就断网：之后只跑测试。"""
        subprocess.run([self.docker, "network", "disconnect", "bridge", self.name],
                       capture_output=True)

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        run = f"timeout -k 5 {timeout_s} bash -c {shlex.quote(script)}"
        full = " && ".join([*self.prelude, run])
        started = time.monotonic()
        try:
            r = subprocess.run([self.docker, "exec", self.name, "bash", "-c", full],
                               capture_output=True, text=True, errors="replace",
                               timeout=timeout_s + 60)
        except subprocess.TimeoutExpired:
            return 124, "", time.monotonic() - started
        return r.returncode, r.stdout + "\n" + r.stderr, time.monotonic() - started

    def write(self, path: str, content: str) -> None:
        subprocess.run([self.docker, "exec", "-i", self.name, "bash", "-c",
                        f"cat > {shlex.quote(path)}"], input=content.encode("utf-8"), check=True)

    def read(self, path: str) -> str:
        r = subprocess.run([self.docker, "exec", self.name, "cat", path], capture_output=True,
                           check=True)
        return r.stdout.decode("utf-8", "replace")

    def git_show(self, path: str) -> str | None:
        """基线提交（HEAD）上的文件内容；新文件返回 None。"""
        r = subprocess.run([self.docker, "exec", "-w", "/testbed", self.name, "git", "show",
                            f"HEAD:{path}"], capture_output=True)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def stop(self) -> None:
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True)


# ---------------------------------------------------------------- 编排


@dataclass
class SweBench:
    """swebench 官方库里用到的三样东西（测试里可以换成假的）。"""

    specs: Callable[[str, str], dict[str, Any]]  # (repo, version) → MAP_REPO_VERSION_TO_SPECS
    directives: Callable[[dict[str, Any]], list[str]]  # get_test_directives(instance)
    parse: Callable[[dict[str, Any], str], dict[str, str]]  # (instance, 日志) → {测试: 状态}

    @classmethod
    def load(cls) -> SweBench:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
        from swebench.harness.test_spec.python import get_test_directives
        from swebench.harness.test_spec.test_spec import make_test_spec

        def parse(instance: dict[str, Any], log: str) -> dict[str, str]:
            spec = make_test_spec({**instance, "PASS_TO_PASS": "[]"})
            status = dict(MAP_REPO_TO_PARSER[instance["repo"]](log, spec))
            if instance["repo"] == "django/django":
                status = {**django_docstring_status(log), **status}
            return status

        return cls(specs=lambda r, v: MAP_REPO_VERSION_TO_SPECS[r][v],
                   directives=get_test_directives, parse=parse)


APPLY_CMDS = (
    "git apply -v {f}",
    "git apply -v --recount {f}",  # 数据集里有的补丁 hunk 行数写错了（corrupt patch）
    "patch --batch --fuzz=5 -p1 -i {f}",  # swebench 官方评测的最后一招
)


def _apply(c: Container, patch: str, name: str) -> str:
    """返回最后成功的那条命令（写进结果，说明补丁是怎么打上的）。"""
    path = f"/tmp/{name}.diff"
    c.write(path, patch)
    out = ""
    for cmd in APPLY_CMDS:
        rc, out, _ = c.sh(cmd.format(f=path))
        if rc == 0:
            return cmd.split(" {f}")[0]
    raise RuntimeError(f"{name} 打不上：{out[-500:]}")


def _use_tests(c: Container, inst: Instance, cond: Condition) -> str:
    """把测试文件恢复到基线提交，再打上这种考卷的 test_patch。"""
    files = sorted({f for k in inst.conditions for f in patch_files(k.test_patch)})
    c.sh("git checkout " + shlex.quote(inst.base_commit) + " -- "
         + " ".join(shlex.quote(f) for f in files) + " 2>/dev/null; true")
    return _apply(c, cond.test_patch, f"tests_{cond.name}")


def run_instance(inst: Instance, sweb: SweBench, *, container: Container | None = None,
                 max_mutants: int = MAX_MUTANTS) -> dict[str, Any]:
    started = time.monotonic()
    specs = sweb.specs(inst.repo, inst.version)
    c = container or Container(IMAGE.format(iid=inst.instance_id),
                               prelude=specs.get("eval_commands", []))
    row: dict[str, Any] = {"instance_id": inst.instance_id, "repo": inst.repo,
                           "group": inst.group, "status": "ok", "conditions": {}}
    c.start()
    try:
        row["gold_apply"] = _apply(c, inst.patch, "gold")
        rc, out, _ = c.sh("python -m pip install -q coverage", timeout_s=300)
        row["coverage_installed"] = rc == 0
        c.offline()
        rc, out, _ = c.sh("python -c 'import sys; print(sys.version_info[0], sys.version_info[1])'")
        py = ".".join(out.split()[:2]) if rc == 0 else None
        files = source_files(inst.patch)
        heads = {f: c.read(f"/testbed/{f}") for f in files}
        bases = {f: c.git_show(f) for f in files}  # 基线提交上的内容（新文件为 None）
        changed = {f: changed_lines(bases[f], heads[f]) for f in files}
        changed = {f: ls for f, ls in changed.items() if ls}
        row["changed_lines"] = sum(len(v) for v in changed.values())
        if not changed:
            row["status"] = "no_source_change"
            return row

        # 每种考卷：基线（标准修复上必须通过）+ 覆盖
        cmds: dict[str, str] = {}
        executed: dict[str, set[int]] = {f: set() for f in changed}
        timeouts: dict[str, int] = {}
        for cond in inst.conditions:
            applied = _use_tests(c, inst, cond)
            directives = sweb.directives({**inst.raw, "test_patch": cond.test_patch})
            cmd = build_test_command(specs["test_cmd"],
                               exam_selectors(inst.repo, cond.fail_to_pass, directives))
            cmds[cond.name] = cmd
            info: dict[str, Any] = {"command": cmd, "exam": cond.fail_to_pass,
                                    "test_patch_apply": applied}
            cov = coverage_command(cmd, [f"/testbed/{f}" for f in changed])
            rc, log, secs = c.sh(cov or cmd, timeout_s=1800)
            baseline = exam_outcome(sweb.parse(inst.raw, log), cond.fail_to_pass)
            if baseline != "pass" and cov is not None:
                info["coverage_failed_tail"] = log[-1000:]
                cov = None
                rc, log, secs = c.sh(cmd, timeout_s=1800)
                baseline = exam_outcome(sweb.parse(inst.raw, log), cond.fail_to_pass)
            info |= {"baseline": baseline, "baseline_s": round(secs, 1),
                     "coverage": cov is not None}
            if baseline != "pass":
                info["log_tail"] = log[-2000:]
            elif cov is not None and row["coverage_installed"]:
                rc, cj, _ = c.sh("python -m coverage json -o - 2>/dev/null || "
                                 "(python -m coverage json -o /tmp/cov.json && cat /tmp/cov.json)")
                got: dict[str, set[int]] = {}
                for f, lines in _coverage_lines(cj).items():
                    rel = f.removeprefix("/testbed/")
                    # coverage 只记语句的第一行：按语句展开，多行语句的后几行也算执行到
                    got[rel] = expand_executed(heads[rel], lines) if rel in heads else lines
                if not any(got.get(f, set()) & changed[f] for f in changed):
                    # 考卷通过了，改动行却一行都没记到：测试在子进程里跑（sympy 的 bin/test
                    # 默认如此，主进程只记到 import 时的行），coverage 看不到。
                    # 覆盖不可用，所有改动行都当目标
                    info["coverage"] = "no_data"
                    for f in changed:
                        executed[f] |= changed[f]
                else:
                    for f in changed:
                        executed[f] |= got.get(f, set())
                info["executed"] = {f: sorted(v) for f, v in executed.items()}
            else:
                # 取不到覆盖：所有改动行都当成执行到了（没执行到的变异体会存活，杀死率偏低）
                for f in changed:
                    executed[f] |= changed[f]
            timeouts[cond.name] = max(MIN_TIMEOUT_S, int(secs * TIMEOUT_FACTOR) + 1)
            row["conditions"][cond.name] = info
        if any(v["baseline"] != "pass" for v in row["conditions"].values()):
            row["status"] = "baseline_failed"
            return row

        targets = {f: changed[f] & executed[f] for f in changed}
        row["executed_lines"] = sum(len(v) for v in targets.values())
        pool = []
        for f, lines in targets.items():
            if lines:
                pool += mutants_for_file(f, heads[f], lines, python=py) or []
        picked = sample(pool, max_mutants, seed=f"calib:{inst.instance_id}")
        row["candidates"] = len(pool)
        row["mutants"] = [{"path": m.path, "line": m.line, "operator": m.operator,
                           "before": m.before, "after": m.after, "outcomes": {}}
                          for m in picked]
        if not picked:
            row["status"] = "no_mutants"
            return row

        # 同一批变异体，每种考卷各跑一遍
        for cond in inst.conditions:
            _use_tests(c, inst, cond)
            for m, rec in zip(picked, row["mutants"], strict=True):
                c.write(f"/testbed/{m.path}", m.code)
                rc, log, secs = c.sh(cmds[cond.name], timeout_s=timeouts[cond.name])
                if rc == 124:
                    outcome = "killed_timeout"
                else:
                    outcome = ("survived" if exam_outcome(sweb.parse(inst.raw, log),
                                                           cond.fail_to_pass) == "pass"
                               else "killed")
                rec["outcomes"][cond.name] = outcome
                c.write(f"/testbed/{m.path}", heads[m.path])
        for cond in inst.conditions:
            outs = [r["outcomes"][cond.name] for r in row["mutants"]]
            killed = sum(o != "survived" for o in outs)
            row["conditions"][cond.name]["killed"] = killed
            row["conditions"][cond.name]["kill_rate"] = round(killed / len(outs), 4)
        return row
    except Exception as e:  # noqa: BLE001 — 单个题出错不影响其他题，写进结果
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {str(e)[:1000]}"
        return row
    finally:
        row["seconds"] = round(time.monotonic() - started, 1)
        c.stop()


def _coverage_lines(text: str) -> dict[str, set[int]]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        data = json.loads(text[start:text.rfind("}") + 1])
    except json.JSONDecodeError:
        return {}
    return {f: set(v.get("executed_lines", [])) for f, v in data.get("files", {}).items()}


# ---------------------------------------------------------------- 汇总


def sign_test(wins: int, losses: int) -> float:
    """双侧符号检验的精确 p 值（平局不计）。"""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)


def permutation_p(a: Sequence[float], b: Sequence[float], *, rounds: int = 20000,
                  seed: int = 0) -> float:
    """两组均值差的双侧置换检验。"""
    if not a or not b:
        return 1.0
    observed = abs(sum(a) / len(a) - sum(b) / len(b))
    pooled = list(a) + list(b)
    rng = random.Random(seed)
    hits = 0
    for _ in range(rounds):
        rng.shuffle(pooled)
        x, y = pooled[:len(a)], pooled[len(a):]
        if abs(sum(x) / len(x) - sum(y) / len(y)) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (rounds + 1)


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["status"] == "ok"]
    paired = [r for r in ok if r["group"] == "utboost"]
    diffs = [r["conditions"]["utboost"]["kill_rate"] - r["conditions"]["official"]["kill_rate"]
             for r in paired]
    up, down = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    # 变异体层面：官方考卷放过、UTBoost 考卷抓到的
    rescued = sum(1 for r in paired for m in r["mutants"]
                  if m["outcomes"]["official"] == "survived"
                  and m["outcomes"]["utboost"] != "survived")
    lost = sum(1 for r in paired for m in r["mutants"]
               if m["outcomes"]["official"] != "survived"
               and m["outcomes"]["utboost"] == "survived")
    ut_off = [r["conditions"]["official"]["kill_rate"] for r in paired]
    ctrl = [r["conditions"]["official"]["kill_rate"] for r in ok if r["group"] == "control"]
    return {
        "n": len(rows), "ok": len(ok),
        "status": {s: sum(r["status"] == s for r in rows)
                   for s in sorted({r["status"] for r in rows})},
        "paired": len(paired), "up": up, "down": down, "tie": len(diffs) - up - down,
        "sign_p": sign_test(up, down),
        "mean_diff": round(sum(diffs) / len(diffs), 4) if diffs else None,
        "rescued": rescued, "lost": lost,
        "utboost_official_mean": round(sum(ut_off) / len(ut_off), 4) if ut_off else None,
        "control_official_mean": round(sum(ctrl) / len(ctrl), 4) if ctrl else None,
        "group_p": permutation_p(ut_off, ctrl),
        "weak_share": {"utboost": _weak(ut_off), "control": _weak(ctrl)},
    }


def _weak(rates: Sequence[float]) -> tuple[int, int, tuple[float, float]] | None:
    if not rates:
        return None
    k = sum(r < 0.5 for r in rates)
    return k, len(rates), wilson(k, len(rates))


def render(rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    s = summarize(rows)
    pct = lambda x: "—" if x is None else f"{x:.0%}"  # noqa: E731
    lines = [
        "# 考卷强度的外部校准：SWE-bench 官方测试 vs UTBoost 增强测试",
        "",
        f"- 时间：{meta.get('started', '—')}；运行：{meta.get('run', '—')}（GitHub Actions）",
        "- 同一个标准修复、同一批变异体（修复改动过且考卷执行到的行，最多 30 个），分别用"
        "官方考卷（官方 FAIL_TO_PASS）和 UTBoost 考卷（加了增强测试）跑，比较杀死率",
        "- 对照组：同仓库分布、UTBoost 没有发现测试不够的 Verified 题（只跑官方考卷）",
        "",
        f"**成对比较（UTBoost 的 {s['paired']} 个题）：加了增强测试后杀死率上升 {s['up']} 个、"
        f"下降 {s['down']} 个、不变 {s['tie']} 个；符号检验 p = {s['sign_p']:.3g}；"
        f"平均上升 {pct(s['mean_diff'])}。变异体层面：官方考卷放过、增强测试抓到的 "
        f"{s['rescued']} 个，反过来的 {s['lost']} 个。**",
        "",
        f"**组间比较：官方考卷的平均杀死率，UTBoost 组 {pct(s['utboost_official_mean'])}，"
        f"对照组 {pct(s['control_official_mean'])}（置换检验 p = {s['group_p']:.3g}）。**",
        "",
        f"- 各状态：{s['status']}",
    ]
    for g, v in s["weak_share"].items():
        if v:
            k, n, (lo, hi) = v
            lines.append(f"- {g} 组官方考卷判为\"弱\"（< 50%）的：{k}/{n}"
                         f"（Wilson {lo:.0%}–{hi:.0%}）")
    lines += ["", "## 逐题", "",
              "| 题 | 组 | 状态 | 改动 / 执行到 | 变异体 | 官方杀死率 | UTBoost 杀死率 | 用时 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["group"], r["instance_id"])):
        conds = r.get("conditions", {})
        off = conds.get("official", {}).get("kill_rate")
        ut = conds.get("utboost", {}).get("kill_rate")
        lines.append(
            f"| {r['instance_id']} | {r['group']} | {r['status']} | "
            f"{r.get('changed_lines', '—')} / {r.get('executed_lines', '—')} | "
            f"{len(r.get('mutants', []))} | {pct(off)} | {pct(ut)} | {r.get('seconds', '—')}s |")
    errors = [r for r in rows if r["status"] != "ok"]
    if errors:
        lines += ["", "## 没算出来的题", ""]
        lines += [f"- {r['instance_id']}：{r['status']} {r.get('error', '')[:200]}" for r in errors]
    return "\n".join(lines) + "\n"
