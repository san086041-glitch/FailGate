"""Docker 沙箱：复现脚本和测试只在这里执行（技术方案 8.4 节）。

两个阶段、两套网络策略：

    install  可以出网（生产环境应接 egress 代理，只放行包源），装依赖
    run      --network none 完全断网，只读根文件系统，执行复现

两个阶段共同的隔离：非 root（1000:1000）、--cap-drop ALL、no-new-privileges、
pids / 内存 / CPU 上限；不挂 Docker socket，不传任何宿主机环境变量。
代码通过 copy_in 复制进工作区卷，宿主机目录从不直接挂载进容器。

执行结果为什么不用 --rm：容器被删掉后就读不到 State.OOMKilled，
而退出码 137 既可能是 OOM，也可能是超时后被 SIGKILL，必须 inspect 才分得清。
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from pydantic import BaseModel

Phase = Literal["install", "run"]

WORKDIR = "/workspace"
SANDBOX_USER = "1000:1000"
LABEL = "warden.sandbox"
OUTPUT_LIMIT = 64 * 1024  # stdout / stderr 各自保留的字节数
_HEAD = 8 * 1024  # 其中开头保留多少；剩下留给结尾（堆栈在最后）
FULL_LOG_LIMIT = 16 * 1024 * 1024  # 完整日志最多落盘这么多，防止无限输出写满磁盘
_HOST_GRACE = 30.0  # 宿主机侧兜底超时比容器内 timeout 多等的秒数

# run 阶段默认允许的命令前缀（按 argv 逐词匹配，不经过 shell）
DEFAULT_RUN_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python",),
    ("python3",),
    ("pytest",),
)


class SandboxError(RuntimeError):
    """沙箱本身出了问题（Docker 不可用、命令不在白名单……），不是被测代码的失败。"""


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "4g"
    cpus: float = 2.0
    pids_install: int = 512
    pids_run: int = 256
    tmpfs_size: str = "512m"


class ExecResult(BaseModel):
    """一次沙箱执行的结果。输出已截断；完整日志在 log_dir（如果配置了产物目录）。"""

    phase: Phase
    argv: list[str]
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    log_dir: str | None = None

    @property
    def infra_failure(self) -> bool:
        """超时或 OOM：说明不了"bug 在不在"，判定器会给出 INCONCLUSIVE。"""
        return self.timed_out or self.oom_killed

    @property
    def failed(self) -> bool:
        return self.exit_code != 0 and not self.infra_failure

    def output_tail(self, lines: int = 60) -> str:
        """给 LLM 看的输出：stderr 优先（堆栈在那里），只取结尾若干行。"""
        text = (self.stdout + "\n" + self.stderr).strip()
        return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------- 纯函数部分


def check_command(argv: Sequence[str], allowed: Sequence[Sequence[str]]) -> None:
    """run 阶段的命令白名单。argv 直接交给 docker，不经过 shell，所以 ; && $() 都没有特殊含义。"""
    if not argv:
        raise SandboxError("命令为空")
    for prefix in allowed:
        if tuple(argv[: len(prefix)]) == tuple(prefix):
            return
    raise SandboxError(f"命令不在白名单内：{shlex.join(argv)}")


def build_run_args(
    *,
    name: str,
    image: str,
    volume: str,
    argv: Sequence[str],
    phase: Phase,
    timeout_s: int,
    limits: SandboxLimits,
    install_network: str,
) -> list[str]:
    """拼出 docker run 的参数（不含 docker 本身）。单独成函数，方便测试逐项核对隔离参数。"""
    args = [
        "run",
        "--name", name,
        "--label", f"{LABEL}=exec",
        "--init",  # tini 当 PID 1：转发信号、回收僵尸进程
        "--user", SANDBOX_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", limits.memory,
        "--memory-swap", limits.memory,  # 与 memory 相同 = 不给 swap，超了就 OOM
        "--cpus", str(limits.cpus),
        "--workdir", WORKDIR,
        "--env", "HOME=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "--volume", f"{volume}:{WORKDIR}",
    ]
    if phase == "install":
        args += ["--network", install_network, "--pids-limit", str(limits.pids_install)]
    else:
        args += [
            "--network", "none",
            "--read-only",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_size}",
            "--pids-limit", str(limits.pids_run),
        ]
    # 容器内的 timeout：到点先 TERM，5 秒后 KILL；退出码 124 表示超时
    args += [image, "timeout", "-k", "5", str(timeout_s), *argv]
    return args


class _Capture:
    """边读边截断：内存里只留开头 _HEAD 字节和结尾若干字节；完整内容（有上限）写到文件。"""

    def __init__(self, sink: IO[bytes] | None) -> None:
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_len = 0
        self.total = 0
        self.sink = sink
        self.written = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.sink is not None and self.written < FULL_LOG_LIMIT:
            part = chunk[: FULL_LOG_LIMIT - self.written]
            self.sink.write(part)
            self.written += len(part)
        room = _HEAD - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail.append(chunk)
            self.tail_len += len(chunk)
            budget = OUTPUT_LIMIT - _HEAD
            while self.tail_len - len(self.tail[0]) >= budget:
                self.tail_len -= len(self.tail.popleft())

    @property
    def truncated(self) -> bool:
        return self.total > OUTPUT_LIMIT

    def text(self) -> str:
        tail = b"".join(self.tail)
        if self.truncated:
            tail = tail[-(OUTPUT_LIMIT - _HEAD):]
            omitted = self.total - len(self.head) - len(tail)
            body = bytes(self.head) + f"\n…[省略 {omitted} 字节]…\n".encode() + tail
        else:
            body = bytes(self.head) + tail
        return body.decode("utf-8", errors="replace")


def find_docker(configured: str = "") -> str | None:
    """找 docker 可执行文件。Windows 上 Docker Desktop 按用户安装时，常常不在当前进程的 PATH 里。"""
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    found = shutil.which("docker")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(local) / "Programs/DockerDesktop/resources/bin/docker.exe" if local else None,
        Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
    ]
    for c in candidates:
        if c is not None and c.exists():
            return str(c)
    return None


# ---------------------------------------------------------------- 与 Docker 交互


class DockerSandbox:
    def __init__(
        self,
        docker_bin: str = "",
        *,
        limits: SandboxLimits | None = None,
        install_network: str = "bridge",
        artifacts_dir: Path | None = None,
    ) -> None:
        self.docker = find_docker(docker_bin) or "docker"
        self.limits = limits or SandboxLimits()
        self.install_network = install_network
        self.artifacts_dir = artifacts_dir

    async def _docker(self, *args: str, limit_s: float = 120.0) -> tuple[int, str, str]:
        """执行一条管理类 docker 命令（输出很小，不需要截断）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise SandboxError(f"找不到 docker：{self.docker}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError as e:
            proc.kill()
            raise SandboxError(f"docker {args[0]} 超时") from e
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def server_version(self) -> str | None:
        """Docker 引擎版本；引擎没启动或找不到 docker 时返回 None。"""
        try:
            code, out, _ = await self._docker(
                "version", "--format", "{{.Server.Version}}", limit_s=20
            )
        except SandboxError:
            return None
        return out.strip() if code == 0 and out.strip() else None

    async def ensure_image(self, image: str) -> None:
        code, _, _ = await self._docker("image", "inspect", image, limit_s=20)
        if code != 0:
            code, _, err = await self._docker("pull", "-q", image, limit_s=600)
            if code != 0:
                raise SandboxError(f"拉取镜像失败：{image}：{err.strip()[:300]}")

    # ---- 工作区卷

    async def create_workspace(self, case_key: str) -> str:
        safe = "".join(c if c.isalnum() else "-" for c in case_key)[:40]
        name = f"warden-ws-{safe}-{uuid.uuid4().hex[:8]}"
        code, _, err = await self._docker(
            "volume", "create",
            "--label", f"{LABEL}=workspace",
            "--label", f"warden.created={int(time.time())}",
            name,
        )
        if code != 0:
            raise SandboxError(f"创建工作区卷失败：{err.strip()}")
        return name

    async def copy_in(self, volume: str, src: Path, image: str) -> None:
        """把宿主机目录的内容复制进工作区卷，并把属主改成沙箱用户。

        docker cp 需要一个容器做中转；复制进来的文件属主是 root，所以再用一个
        只带 CHOWN 能力、断网的短命容器改属主，否则 1000 用户写不了工作区。
        """
        if not await asyncio.to_thread(src.is_dir):
            raise SandboxError(f"不是目录：{src}")
        helper = f"warden-cp-{uuid.uuid4().hex[:8]}"
        code, _, err = await self._docker(
            "create", "--name", helper, "--label", f"{LABEL}=helper",
            "--network", "none", "--volume", f"{volume}:{WORKDIR}", image, "true",
        )
        if code != 0:
            raise SandboxError(f"创建中转容器失败：{err.strip()}")
        try:
            code, _, err = await self._docker("cp", f"{src}{os.sep}.", f"{helper}:{WORKDIR}")
            if code != 0:
                raise SandboxError(f"复制代码失败：{err.strip()}")
        finally:
            await self._docker("rm", "-f", helper)
        code, _, err = await self._docker(
            "run", "--rm", "--label", f"{LABEL}=helper", "--network", "none",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges",
            "--volume", f"{volume}:{WORKDIR}", image, "chown", "-R", SANDBOX_USER, WORKDIR,
        )
        if code != 0:
            raise SandboxError(f"修改工作区属主失败：{err.strip()}")

    async def remove_workspace(self, volume: str) -> None:
        await self._docker("volume", "rm", "-f", volume)

    async def prune_workspaces(self, older_than_s: float) -> list[str]:
        """删除创建时间早于 older_than_s 秒之前的工作区卷（TTL 兜底，正常应在 Case 结束时删）。"""
        code, out, _ = await self._docker(
            "volume", "ls", "--filter", f"label={LABEL}=workspace", "--format", "{{.Name}}"
        )
        if code != 0:
            return []
        names = [n for n in out.split() if n]
        if not names:
            return []
        _, info, _ = await self._docker("volume", "inspect", *names)
        cutoff = time.time() - older_than_s
        stale = [
            v["Name"] for v in json.loads(info or "[]")
            if int((v.get("Labels") or {}).get("warden.created", "0")) < cutoff
        ]
        for name in stale:
            await self.remove_workspace(name)
        return stale

    # ---- 执行

    async def install(
        self, image: str, volume: str, argv: Sequence[str], *, timeout_s: int = 600
    ) -> ExecResult:
        """装依赖。可以出网，根文件系统可写，但仍是非 root、无 capabilities。"""
        return await self._exec("install", image, volume, argv, timeout_s)

    async def run(
        self,
        image: str,
        volume: str,
        argv: Sequence[str],
        *,
        timeout_s: int = 120,
        allowed: Sequence[Sequence[str]] = DEFAULT_RUN_PREFIXES,
    ) -> ExecResult:
        """执行复现：断网、只读根文件系统，命令必须在白名单内。"""
        check_command(argv, allowed)
        return await self._exec("run", image, volume, argv, timeout_s)

    async def _exec(
        self, phase: Phase, image: str, volume: str, argv: Sequence[str], timeout_s: int
    ) -> ExecResult:
        name = f"warden-{phase}-{uuid.uuid4().hex[:10]}"
        args = build_run_args(
            name=name, image=image, volume=volume, argv=argv, phase=phase,
            timeout_s=timeout_s, limits=self.limits, install_network=self.install_network,
        )
        log_dir, out_f, err_f = _open_logs(self.artifacts_dir, name)
        out_cap, err_cap = _Capture(out_f), _Capture(err_f)

        start = time.monotonic()
        host_timeout = False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise SandboxError(f"找不到 docker：{self.docker}") from e
        try:
            assert proc.stdout is not None and proc.stderr is not None
            pump = asyncio.gather(
                _pump(proc.stdout, out_cap), _pump(proc.stderr, err_cap), proc.wait()
            )
            try:
                await asyncio.wait_for(pump, timeout_s + _HOST_GRACE)
            except TimeoutError:
                # 容器内的 timeout 没生效（镜像里没有 timeout、或 docker 卡住）：从外面杀
                host_timeout = True
                await self._docker("kill", name, limit_s=30)
                await proc.wait()
            duration = time.monotonic() - start
            exit_code = proc.returncode if proc.returncode is not None else -1
            oom = await self._oom_killed(name)
        finally:
            for f in (out_f, err_f):
                if f is not None:
                    f.close()
            await self._docker("rm", "-f", name, limit_s=60)

        if exit_code == 125 and oom is None:
            # docker run 自己失败了（镜像不存在、参数错误），容器根本没启动
            raise SandboxError(f"docker run 失败：{err_cap.text().strip()[:500]}")
        oom_killed = bool(oom)
        # 124：timeout 发 TERM 后进程退出；137 且不是 OOM 且用满了时间：-k 之后被 KILL
        timed_out = host_timeout or exit_code == 124 or (
            exit_code == 137 and not oom_killed and duration >= timeout_s
        )
        if log_dir is not None:
            (log_dir / "meta.json").write_text(
                json.dumps(
                    {"phase": phase, "argv": list(argv), "image": image, "exit_code": exit_code,
                     "timed_out": timed_out, "oom_killed": oom_killed,
                     "duration_s": round(duration, 3)},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        return ExecResult(
            phase=phase,
            argv=list(argv),
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            duration_s=round(duration, 3),
            stdout=out_cap.text(),
            stderr=err_cap.text(),
            truncated=out_cap.truncated or err_cap.truncated,
            log_dir=str(log_dir) if log_dir else None,
        )

    async def _oom_killed(self, name: str) -> bool | None:
        """容器不存在（没启动成功）时返回 None。"""
        code, out, _ = await self._docker(
            "inspect", "--format", "{{.State.OOMKilled}}", name, limit_s=30
        )
        if code != 0:
            return None
        return out.strip() == "true"


def _open_logs(
    artifacts_dir: Path | None, name: str
) -> tuple[Path | None, IO[bytes] | None, IO[bytes] | None]:
    """本地小文件，同步打开即可（写入量由 FULL_LOG_LIMIT 封顶）。"""
    if artifacts_dir is None:
        return None, None, None
    log_dir = artifacts_dir / name
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir, open(log_dir / "stdout.log", "wb"), open(log_dir / "stderr.log", "wb")


async def _pump(stream: asyncio.StreamReader, cap: _Capture) -> None:
    while chunk := await stream.read(65536):
        cap.feed(chunk)
