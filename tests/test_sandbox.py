"""沙箱：前半部分是纯函数测试；后半部分标记为 docker，真的起容器（没有 Docker 时跳过）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from failgate.repro.judge import VerdictKind, assess
from failgate.repro.sandbox import (
    OUTPUT_LIMIT,
    DockerSandbox,
    SandboxError,
    SandboxLimits,
    _Capture,
    build_run_args,
    check_command,
    classify_exit,
    find_docker,
)
from failgate.repro.selfcheck import self_check

IMAGE = "python:3.12-slim"


def args_for(phase: str) -> list[str]:
    return build_run_args(
        name="n", image=IMAGE, volume="ws", argv=["python", "repro.py"], phase=phase,  # type: ignore[arg-type]
        timeout_s=120, limits=SandboxLimits(), install_network="failgate-egress",
    )


def flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


@pytest.mark.parametrize("phase", ["install", "run"])
def test_isolation_flags_present_in_both_phases(phase):
    a = args_for(phase)
    assert flag(a, "--user") == ["1000:1000"]
    assert flag(a, "--cap-drop") == ["ALL"]
    assert flag(a, "--security-opt") == ["no-new-privileges"]
    assert flag(a, "--memory") == flag(a, "--memory-swap") == ["4g"]
    assert "--privileged" not in a and "--rm" not in a
    # 不挂 Docker socket、不挂宿主机目录：唯一的卷是命名工作区卷
    assert flag(a, "--volume") == ["ws:/workspace"]
    assert not any("docker.sock" in x for x in a)
    # 命令被容器内 timeout 包住
    i = a.index(IMAGE)
    assert a[i + 1 : i + 5] == ["timeout", "-k", "5", "120"] and a[-2:] == ["python", "repro.py"]


def test_run_phase_is_offline_and_readonly_install_is_not():
    run, install = args_for("run"), args_for("install")
    assert flag(run, "--network") == ["none"] and "--read-only" in run
    assert flag(run, "--pids-limit") == ["256"]
    assert flag(install, "--network") == ["failgate-egress"] and "--read-only" not in install
    assert flag(install, "--pids-limit") == ["512"]


def test_command_allowlist_matches_argv_prefix():
    allowed = [("python",), ("python", "-m", "pytest")]
    check_command(["python", "repro.py"], allowed)
    check_command(["python", "-m", "pytest", "-x"], allowed)
    for bad in (["sh", "-c", "python x.py"], ["pip", "install", "x"], ["pythonx"], []):
        with pytest.raises(SandboxError):
            check_command(bad, allowed)


def test_capture_keeps_head_and_tail_and_marks_truncation(tmp_path: Path):
    with open(tmp_path / "log", "wb") as f:
        cap = _Capture(f)
        cap.feed(b"HEAD" + b"a" * 10_000)
        for _ in range(100):
            cap.feed(b"b" * 4096)
        cap.feed(b"KeyError: 'the end'")
    text = cap.text()
    assert cap.truncated and text.startswith("HEAD") and text.endswith("KeyError: 'the end'")
    assert "省略" in text and len(text.encode()) < OUTPUT_LIMIT + 100
    assert (tmp_path / "log").stat().st_size == cap.total


def test_capture_small_output_untouched():
    cap = _Capture(None)
    for part in (b"hello ", b"world"):
        cap.feed(part)
    assert cap.text() == "hello world" and not cap.truncated


@pytest.mark.parametrize(
    ("exit_code", "oom_reported", "duration", "host", "want"),
    [
        (0, False, 1.0, False, (False, None)),
        (1, False, 1.0, False, (False, None)),
        (124, False, 120.2, False, (True, None)),  # 容器内 timeout 发 TERM
        (137, False, 125.1, False, (True, None)),  # TERM 不理，-k 5 之后被 KILL
        (137, True, 3.0, False, (False, "docker")),  # Docker 报告了 OOMKilled
        # CI 上实测：OOM 杀的是 tini 下面的孙进程，OOMKilled 没被标上
        (137, False, 3.0, False, (False, "inferred")),
        (-1, False, 150.0, True, (True, None)),  # 宿主机兜底 docker kill
    ],
)
def test_classify_exit(exit_code, oom_reported, duration, host, want):
    got = classify_exit(
        exit_code, oom_reported=oom_reported, duration_s=duration, timeout_s=120,
        host_timeout=host,
    )
    assert got == want


def test_find_docker_configured_missing():
    assert find_docker("Z:/definitely/not/docker.exe") is None


# ---------------------------------------------------------------- 真实容器


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    asyncio.run(sb.ensure_image(IMAGE))
    return sb


@pytest.mark.docker
async def test_self_check_all_green(sandbox: DockerSandbox):
    items = await self_check(sandbox, IMAGE)
    failed = [f"{i.name}: {i.detail}" for i in items if not i.ok]
    assert not failed, failed


REPRO = """\
import mylib
mylib.parse({"title": "x"})
"""
MYLIB = """\
def parse(d):
    return _field(d, "name")

def _field(d, key):
    return d[key]
"""
REPORTED = """\
Traceback (most recent call last):
  File "C:\\Users\\bob\\app.py", line 9, in <module>
  File "C:\\Users\\bob\\venv\\site-packages\\mylib\\__init__.py", line 2, in parse
  File "C:\\Users\\bob\\venv\\site-packages\\mylib\\__init__.py", line 5, in _field
KeyError: 'name'
"""


@pytest.mark.docker
async def test_end_to_end_repro_is_judged_reproduced(sandbox: DockerSandbox, tmp_path: Path):
    (tmp_path / "mylib").mkdir()
    (tmp_path / "mylib" / "__init__.py").write_text(MYLIB)
    (tmp_path / "repro.py").write_text(REPRO)
    ws = await sandbox.create_workspace("test-e2e")
    try:
        await sandbox.copy_in(ws, tmp_path, IMAGE)

        async def once():
            return await sandbox.run(IMAGE, ws, ["python", "repro.py"], timeout_s=30)

        first = await once()
        assert first.failed and "KeyError: 'name'" in first.stderr
        assert first.log_dir and (Path(first.log_dir) / "stderr.log").exists()
        v = await assess(first, once, reported_traceback=REPORTED, package="mylib")
        assert v.kind == VerdictKind.REPRODUCED and v.runs == 4 and v.match == 1.0
    finally:
        await sandbox.remove_workspace(ws)


@pytest.mark.docker
async def test_huge_output_is_truncated(sandbox: DockerSandbox, tmp_path: Path):
    (tmp_path / "spam.py").write_text("import sys\nsys.stdout.write('x' * 2_000_000)\nprint('END')")
    ws = await sandbox.create_workspace("test-spam")
    try:
        await sandbox.copy_in(ws, tmp_path, IMAGE)
        r = await sandbox.run(IMAGE, ws, ["python", "spam.py"], timeout_s=30)
    finally:
        await sandbox.remove_workspace(ws)
    assert r.exit_code == 0 and r.truncated and r.stdout.rstrip().endswith("END")
    assert len(r.stdout) < OUTPUT_LIMIT + 100


@pytest.mark.docker
async def test_disallowed_command_never_reaches_docker(sandbox: DockerSandbox):
    with pytest.raises(SandboxError):
        await sandbox.run(IMAGE, "unused", ["sh", "-c", "curl evil"])
