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
import re
import shlex
import shutil
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Literal

from pydantic import BaseModel

from failgate import tracing
from failgate.repro import egress as egress_mod
from failgate.repro.egress import MODE as EGRESS_MODE
from failgate.repro.egress import allow_list, proxy_env

Phase = Literal["install", "run"]

WORKDIR = "/workspace"
SANDBOX_USER = "1000:1000"
LABEL = "failgate.sandbox"
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
# run 阶段允许传的环境变量：只能把 PYTHONPATH 指向工作区里的目录
_WS_PATH = r"/workspace(?:/(?!\.\.?(?:[/:]|$))[\w.-]+)*"  # 不允许 . 和 .. 这两种路径段
RUN_ENV = re.compile(rf"PYTHONPATH={_WS_PATH}(?::{_WS_PATH})*")
# install 阶段只允许装包
DEFAULT_INSTALL_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pip", "install"),
    ("python", "-m", "pip", "install"),
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


@dataclass
class PruneResult:
    """prune_workspaces 的结果：只统计真的删掉的，失败的单独列出（名字, 原因）。"""

    containers: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


class ExecResult(BaseModel):
    """一次沙箱执行的结果。输出已截断；完整日志在 log_dir（如果配置了产物目录）。"""

    phase: Phase
    argv: list[str]
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False
    oom_source: Literal["docker", "inferred"] | None = None  # 见 classify_exit
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
    env: Sequence[str] = (),
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
    for kv in env:
        args += ["--env", kv]
    if phase == "install":
        args += [
            "--network", install_network,
            "--pids-limit", str(limits.pids_install),
            # 不写 pip 缓存：install 容器会被 commit 成环境镜像，缓存会白白撑大镜像
            "--env", "PIP_NO_CACHE_DIR=1",
        ]
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


def classify_exit(
    exit_code: int,
    *,
    oom_reported: bool,
    duration_s: float,
    timeout_s: int,
    host_timeout: bool,
) -> tuple[bool, Literal["docker", "inferred"] | None]:
    """返回 (是否超时, OOM 的判断依据)。

    State.OOMKilled 不总是可靠：我们用 --init，被内核 OOM 杀掉的是 tini 下面的孙进程，
    Docker 读取 cgroup 的 OOM 事件有竞争，CI 上实测出现过 exit=137 但 OOMKilled=False。
    所以再加一条推断：容器内的 timeout 只会在 timeout_s 之后才发 KILL，宿主机兜底另有
    host_timeout 标记；如果在这之前就被 SIGKILL（137），沙箱里又没有别人会发这个信号，
    那就是 OOM killer。（被测代码自己 kill -9 自己也会落到这里，但判定器对 OOM 给的是
    INCONCLUSIVE，误判的方向是保守的。）
    """
    if host_timeout:
        return True, None
    if oom_reported:
        return False, "docker"
    if exit_code == 124:
        return True, None
    if exit_code == 137:
        if duration_s >= timeout_s:
            return True, None  # -k 之后被 KILL
        return False, "inferred"
    return False, None


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


def docker_env(docker: str) -> dict[str, str] | None:
    """docker 客户端子进程的环境变量：把 docker 所在目录放到 PATH 最前面。

    docker 拉镜像时要调用同目录下的凭据助手（Windows 上是 docker-credential-desktop.exe），
    它是按 PATH 查找的。Docker Desktop 按用户安装时这个目录常常不在 PATH 里，
    表现为"已有的镜像都能用，一拉新镜像就报 docker-credential-desktop not found"。
    """
    folder = Path(docker).parent
    if not Path(docker).is_absolute() or not folder.is_dir():
        return None  # 用继承的环境
    env = dict(os.environ)
    env["PATH"] = str(folder) + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- 与 Docker 交互


class DockerSandbox:
    def __init__(
        self,
        docker_bin: str = "",
        *,
        limits: SandboxLimits | None = None,
        install_network: str = EGRESS_MODE,
        artifacts_dir: Path | None = None,
        egress_allow: Sequence[str] | None = None,
    ) -> None:
        self.docker = find_docker(docker_bin) or "docker"
        self.limits = limits or SandboxLimits()
        # "egress"：install 阶段走白名单代理（ADR 0042）；其他值原样当作 docker 网络名
        self.install_network = install_network
        self.egress_allow = list(egress_allow) if egress_allow is not None else allow_list()
        self.artifacts_dir = artifacts_dir
        self._env = docker_env(self.docker)
        self._egress_lock = asyncio.Lock()
        self._egress_ready = False

    @classmethod
    def from_settings(cls, s: Any) -> DockerSandbox:
        """按配置建沙箱（流水线、CLI、MCP 共用；s 是 Settings）。"""
        return cls(
            s.docker_bin,
            limits=SandboxLimits(memory=s.sandbox_memory, cpus=s.sandbox_cpus),
            install_network=s.sandbox_install_network,
            artifacts_dir=Path(s.sandbox_artifacts_dir),
            egress_allow=allow_list([x for x in s.sandbox_egress_allow.split(",") if x.strip()],
                                    s.pip_index_url),
        )

    @property
    def egress(self) -> bool:
        return self.install_network == EGRESS_MODE

    async def ensure_egress(self) -> None:
        """建好白名单代理：镜像、internal 网络、代理容器（白名单变了就重建）。幂等。"""
        if not self.egress:
            return
        async with self._egress_lock:
            if self._egress_ready:
                return
            if await self.image_info(egress_mod.IMAGE) is None:
                await self.build_image(egress_mod.IMAGE, egress_mod.DOCKERFILE, limit_s=600)
            code, _, _ = await self._docker("network", "inspect", egress_mod.NETWORK, limit_s=20)
            if code != 0:
                code, _, err = await self._docker(
                    "network", "create", "--internal", "--label", f"{LABEL}=egress",
                    egress_mod.NETWORK, limit_s=30)
                if code != 0:
                    raise SandboxError(f"建 egress 网络失败：{err.strip()[:300]}")
            want = f"ALLOW={','.join(self.egress_allow)}"
            code, out, _ = await self._docker(
                "inspect", "--format", "{{.State.Running}} {{json .Config.Env}}",
                egress_mod.CONTAINER, limit_s=20)
            running, _, env_json = out.strip().partition(" ")
            try:
                current = json.loads(env_json) if code == 0 else []
            except json.JSONDecodeError:
                current = []
            # 精确比较：子串匹配会把 "ALLOW=pypi.org,old" 当成已经是 "ALLOW=pypi.org"
            if code == 0 and want not in current:
                await self._docker("rm", "-f", egress_mod.CONTAINER, limit_s=60)
                code = 1
            if code != 0:
                code, _, err = await self._docker(*egress_mod.run_args(self.egress_allow),
                                                  limit_s=60)
                if code != 0:
                    raise SandboxError(f"启动 egress 代理失败：{err.strip()[:300]}")
            elif running != "true":
                await self._docker("start", egress_mod.CONTAINER, limit_s=60)
            code, _, err = await self._docker(
                "network", "connect", "--alias", egress_mod.ALIAS, egress_mod.NETWORK,
                egress_mod.CONTAINER, limit_s=30)
            if code != 0 and "already exists" not in err:
                raise SandboxError(f"代理接入 egress 网络失败：{err.strip()[:300]}")
            self._egress_ready = True

    async def _docker(
        self, *args: str, limit_s: float = 120.0, stdin: bytes | None = None
    ) -> tuple[int, str, str]:
        """执行一条管理类 docker 命令（输出很小，不需要截断）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except FileNotFoundError as e:
            raise SandboxError(f"找不到 docker：{self.docker}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), limit_s)
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

    # ---- 镜像

    async def image_info(self, ref: str) -> tuple[str, int] | None:
        """(镜像 ID, 字节数)；镜像不存在返回 None。"""
        code, out, _ = await self._docker(
            "image", "inspect", "--format", "{{.Id}} {{.Size}}", ref, limit_s=30
        )
        if code != 0 or not out.strip():
            return None
        image_id, size = out.split()
        return image_id, int(size)

    async def build_image(self, tag: str, dockerfile: str, *, limit_s: float = 900) -> None:
        """用我们自己写的（可信的）Dockerfile 构建镜像，从 stdin 传入，不需要构建上下文。"""
        code, _, err = await self._docker(
            "build", "--quiet", "--label", f"{LABEL}=base", "-t", tag, "-",
            limit_s=limit_s, stdin=dockerfile.encode(),
        )
        if code != 0:
            raise SandboxError(f"构建镜像失败：{tag}：{err.strip()[-500:]}")

    async def remove_image(self, ref: str) -> None:
        await self._docker("image", "rm", "-f", ref, limit_s=120)

    # ---- 工作区卷

    async def create_workspace(self, case_key: str) -> str:
        safe = "".join(c if c.isalnum() else "-" for c in case_key)[:40]
        name = f"failgate-ws-{safe}-{uuid.uuid4().hex[:8]}"
        code, _, err = await self._docker(
            "volume", "create",
            "--label", f"{LABEL}=workspace",
            "--label", f"failgate.created={int(time.time())}",
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
        helper = f"failgate-cp-{uuid.uuid4().hex[:8]}"
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
            # 显式 root：环境镜像的默认用户是 1000，不指定的话 chown 会失败
            "run", "--rm", "--label", f"{LABEL}=helper", "--network", "none", "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges",
            "--volume", f"{volume}:{WORKDIR}", image, "chown", "-R", SANDBOX_USER, WORKDIR,
        )
        if code != 0:
            raise SandboxError(f"修改工作区属主失败：{err.strip()}")

    async def remove_workspace(self, volume: str) -> None:
        await self._docker("volume", "rm", "-f", volume)

    async def prune_workspaces(self, older_than_s: float) -> PruneResult:
        """TTL 兜底清理（正常应在 Case 结束时删）。

        先删结束时间早于 TTL 的已退出沙箱容器（进程被硬杀时会留下，它们一直占着卷），
        再删创建时间早于 TTL 的工作区卷。运行中的容器一律不动；卷仍被占用时删不掉，
        记进 failed 而不是算作已删除。
        """
        result = PruneResult()
        cutoff = time.time() - older_than_s
        await self._prune_exec_containers(cutoff, result)

        code, out, _ = await self._docker(
            "volume", "ls", "--filter", f"label={LABEL}=workspace", "--format", "{{.Name}}"
        )
        if code != 0:
            return result
        names = [n for n in out.split() if n]
        if not names:
            return result
        _, info, _ = await self._docker("volume", "inspect", *names)
        stale = [
            v["Name"] for v in json.loads(info or "[]")
            if int((v.get("Labels") or {}).get("failgate.created", "0")) < cutoff
        ]
        for name in stale:
            code, _, err = await self._docker("volume", "rm", name)
            if code == 0:
                result.volumes.append(name)
            else:
                result.failed.append((name, err.strip() or f"exit {code}"))
        return result

    async def _prune_exec_containers(self, cutoff: float, result: PruneResult) -> None:
        code, out, _ = await self._docker(
            "ps", "-a", "--filter", f"label={LABEL}=exec",
            "--filter", "status=exited", "--filter", "status=dead",
            "--format", "{{.ID}}",
        )
        ids = [i for i in out.split() if i] if code == 0 else []
        if not ids:
            return
        _, info, _ = await self._docker("container", "inspect", *ids)
        for c in json.loads(info or "[]"):
            state = c.get("State") or {}
            # 两次调用之间可能又被启动了：只认非运行状态
            if state.get("Running") or state.get("Status") not in ("exited", "dead"):
                continue
            if _docker_time(state.get("FinishedAt", "")) >= cutoff:
                continue
            name = str(c.get("Name", "")).lstrip("/") or c["Id"][:12]
            # 不加 -f：万一此刻容器在运行，docker 会拒绝删除
            code, _, err = await self._docker("rm", c["Id"])
            if code == 0:
                result.containers.append(name)
            else:
                result.failed.append((name, err.strip() or f"exit {code}"))

    # ---- 执行

    async def install(
        self,
        image: str,
        volume: str,
        argv: Sequence[str],
        *,
        timeout_s: int = 600,
        allowed: Sequence[Sequence[str]] = DEFAULT_INSTALL_PREFIXES,
        env: Sequence[str] = (),
        commit_to: str | None = None,
    ) -> ExecResult:
        """装依赖。可以出网，根文件系统可写，但仍是非 root、无 capabilities。

        commit_to：安装成功后把容器 commit 成这个镜像（环境缓存用）。
        """
        check_command(argv, allowed)
        if self.egress:
            await self.ensure_egress()
            env = [*env, *proxy_env()]
        return await self._exec(
            "install", image, volume, argv, timeout_s, env=env, commit_to=commit_to
        )

    async def run(
        self,
        image: str,
        volume: str,
        argv: Sequence[str],
        *,
        timeout_s: int = 120,
        allowed: Sequence[Sequence[str]] = DEFAULT_RUN_PREFIXES,
        env: Sequence[str] = (),
    ) -> ExecResult:
        """执行复现：断网、只读根文件系统，命令必须在白名单内。

        env 只允许把 PYTHONPATH 指向工作区（考卷强度评估让测试导入工作区里的源码副本）。"""
        check_command(argv, allowed)
        for kv in env:
            if not RUN_ENV.fullmatch(kv):
                raise SandboxError(f"环境变量不在白名单内：{kv}")
        return await self._exec("run", image, volume, argv, timeout_s, env=env)

    async def _exec(
        self,
        phase: Phase,
        image: str,
        volume: str,
        argv: Sequence[str],
        timeout_s: int,
        *,
        env: Sequence[str] = (),
        commit_to: str | None = None,
    ) -> ExecResult:
        # 一次容器执行一个 span（ADR 0025）：命令只记前几段，不记脚本内容
        with tracing.tracer.start_as_current_span(
            f"sandbox {phase}",
            attributes={"failgate.sandbox.phase": phase, "failgate.sandbox.image": image,
                        "failgate.sandbox.argv": shlex.join(argv[:4])[:200],
                        "failgate.sandbox.timeout_s": timeout_s},
        ) as span:
            result = await self._exec_inner(phase, image, volume, argv, timeout_s,
                                            env=env, commit_to=commit_to)
            span.set_attributes({
                "failgate.sandbox.exit_code": result.exit_code,
                "failgate.sandbox.duration_s": result.duration_s,
                "failgate.sandbox.timed_out": result.timed_out,
                "failgate.sandbox.oom_killed": bool(result.oom_killed),
            })
            # 允许记内容时：完整命令 + 输出的结尾（pytest 的结论和堆栈都在最后）
            tracing.set_io(span, input=shlex.join(argv), output={
                "exit_code": result.exit_code,
                "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:],
            })
            return result

    async def _exec_inner(
        self,
        phase: Phase,
        image: str,
        volume: str,
        argv: Sequence[str],
        timeout_s: int,
        *,
        env: Sequence[str] = (),
        commit_to: str | None = None,
    ) -> ExecResult:
        name = f"failgate-{phase}-{uuid.uuid4().hex[:10]}"
        args = build_run_args(
            name=name, image=image, volume=volume, argv=argv, phase=phase,
            timeout_s=timeout_s, limits=self.limits,
            install_network=egress_mod.NETWORK if self.egress else self.install_network,
            env=env,
        )
        log_dir, out_f, err_f = _open_logs(self.artifacts_dir, name)
        out_cap, err_cap = _Capture(out_f), _Capture(err_f)

        start = time.monotonic()
        host_timeout = False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self._env,
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
            if commit_to and exit_code == 0 and oom is False and not host_timeout:
                # 卷不会进镜像；默认命令改成 python，免得镜像里留着这次的安装命令
                code, _, err = await self._docker(
                    "commit", "--change", 'CMD ["python"]',
                    "--change", f"LABEL {LABEL}=env", name, commit_to, limit_s=300,
                )
                if code != 0:
                    raise SandboxError(f"commit 环境镜像失败：{err.strip()[:300]}")
        finally:
            for f in (out_f, err_f):
                if f is not None:
                    f.close()
            await self._docker("rm", "-f", name, limit_s=60)

        if exit_code == 125 and oom is None:
            # docker run 自己失败了（镜像不存在、参数错误），容器根本没启动
            raise SandboxError(f"docker run 失败：{err_cap.text().strip()[:500]}")
        timed_out, oom_source = classify_exit(
            exit_code, oom_reported=bool(oom), duration_s=duration, timeout_s=timeout_s,
            host_timeout=host_timeout,
        )
        oom_killed = oom_source is not None
        if log_dir is not None:
            (log_dir / "meta.json").write_text(
                json.dumps(
                    {"phase": phase, "argv": list(argv), "image": image, "exit_code": exit_code,
                     "timed_out": timed_out, "oom_killed": oom_killed, "oom_source": oom_source,
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
            oom_source=oom_source,
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


def _docker_time(value: str) -> float:
    """解析 docker 的 RFC3339 时间（纳秒精度，如 2026-09-30T12:00:00.123456789Z）为时间戳。

    解析不了（或是零值 0001-01-01）时返回 0，即视为很久以前。
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$", value)
    if not m:
        return 0.0
    tz = m.group(2) or "Z"
    try:
        dt = datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz))
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


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
