"""安装阶段的出网白名单（ADR 0042）：不起容器的部分。真实容器的验证在 sandbox check 里。"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from failgate.repro import egress
from failgate.repro.sandbox import DockerSandbox, ExecResult


def test_allow_list_keeps_pypi_adds_index_host_and_drops_junk():
    got = egress.allow_list(["Mirror.Example.COM", "bad host", "", "pypi.org", "localhost"],
                            "https://pip.corp.example/simple")
    assert got == ["pypi.org", "files.pythonhosted.org", "mirror.example.com",
                   "pip.corp.example"]


def test_proxy_container_is_locked_down_and_carries_the_allow_list():
    args = egress.run_args(["pypi.org", "files.pythonhosted.org"])
    for flag in ("--cap-drop", "--read-only", "--pids-limit", "--memory"):
        assert flag in args
    assert "ALLOW=pypi.org,files.pythonhosted.org" in args and args[-1] == egress.IMAGE
    assert args[args.index("--network") + 1] == "bridge"  # internal 网络之后再 connect


def test_entry_script_denies_by_default_and_only_connects_to_443():
    script = base64.b64decode(egress._ENTRY_B64).decode()
    assert "FilterDefaultDeny Yes" in script and "ConnectPort 443" in script
    assert "FilterURLs Off" in script  # 按域名过滤，不按完整 URL
    assert "USER nobody" in egress.DOCKERFILE


def test_proxy_env_sets_both_spellings():
    env = egress.proxy_env()
    url = "http://egress-proxy:8888"
    assert f"HTTPS_PROXY={url}" in env and f"https_proxy={url}" in env


def settings(**kw: Any) -> Any:
    base = dict(docker_bin="", sandbox_memory="4g", sandbox_cpus=2.0,
                sandbox_install_network="egress", sandbox_artifacts_dir="./artifacts",
                sandbox_egress_allow="extra.example.org", pip_index_url="")
    return SimpleNamespace(**{**base, **kw})


def test_from_settings_builds_the_allow_list():
    sb = DockerSandbox.from_settings(settings())
    assert sb.egress and sb.egress_allow == ["pypi.org", "files.pythonhosted.org",
                                             "extra.example.org"]
    assert not DockerSandbox.from_settings(settings(sandbox_install_network="bridge")).egress
    assert DockerSandbox().egress  # 类的默认值也是 egress：默认就是安全的那一种


class FakeDocker(DockerSandbox):
    """记录 docker 管理命令；state 决定 inspect 的返回。"""

    def __init__(self, *, image: bool, network: bool, container: str | None) -> None:
        super().__init__(egress_allow=["pypi.org"])
        self.calls: list[tuple[str, ...]] = []
        self.image, self.network, self.container = image, network, container

    async def _docker(self, *args: str, limit_s: float = 120.0,
                      stdin: bytes | None = None) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            return (0, "sha256:x 123", "") if self.image else (1, "", "")
        if args[:2] == ("network", "inspect"):
            return (0, "", "") if self.network else (1, "", "")
        if args[0] == "inspect":
            return (0, self.container, "") if self.container else (1, "", "")
        return 0, "", ""


async def test_ensure_egress_builds_everything_from_scratch_once():
    sb = FakeDocker(image=False, network=False, container=None)
    await sb.ensure_egress()
    await sb.ensure_egress()  # 第二次什么都不做
    verbs = [c[0] if c[0] != "network" else f"network {c[1]}" for c in sb.calls]
    assert verbs == ["image", "build", "network inspect", "network create", "inspect", "run",
                     "network connect"]
    create = next(c for c in sb.calls if c[:2] == ("network", "create"))
    assert "--internal" in create
    connect = next(c for c in sb.calls if c[:2] == ("network", "connect"))
    assert connect[connect.index("--alias") + 1] == egress.ALIAS


async def test_ensure_egress_recreates_the_proxy_when_the_allow_list_changed():
    sb = FakeDocker(image=True, network=True, container='true ["ALLOW=pypi.org,old.example"]')
    await sb.ensure_egress()
    verbs = [c[0] for c in sb.calls]
    assert ("rm", "-f", egress.CONTAINER) in sb.calls and verbs.index("rm") < verbs.index("run")
    sb = FakeDocker(image=True, network=True, container='false ["ALLOW=pypi.org"]')
    await sb.ensure_egress()
    assert ("start", egress.CONTAINER) in sb.calls and "run" not in [c[0] for c in sb.calls]


async def test_install_goes_through_the_proxy_network():
    sb = FakeDocker(image=True, network=True, container='true ["ALLOW=pypi.org"]')
    seen: dict[str, Any] = {}

    async def fake_exec(phase: str, image: str, volume: str, argv: Any, timeout_s: int, *,
                        env: Any = (), commit_to: Any = None) -> ExecResult:
        seen.update(phase=phase, env=list(env))
        return ExecResult(phase="install", argv=list(argv), exit_code=0)

    sb._exec = fake_exec  # type: ignore[method-assign]
    await sb.install("img", "vol", ["pip", "install", "x"],
                     env=["PIP_INDEX_URL=https://pypi.org/simple"], allowed=(("pip", "install"),))
    assert seen["phase"] == "install"
    assert "HTTPS_PROXY=http://egress-proxy:8888" in seen["env"]
    assert "PIP_INDEX_URL=https://pypi.org/simple" in seen["env"]
