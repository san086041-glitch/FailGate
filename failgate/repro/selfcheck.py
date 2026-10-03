"""沙箱自检：真的起容器，逐项确认隔离参数生效（failgate sandbox check 和集成测试共用）。

只看 docker run 的参数不够：Docker Desktop、rootless Docker、老内核上同样的参数效果可能不同，
所以在容器里面从 /proc 和 cgroup 读回实际状态。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from failgate.repro.sandbox import DockerSandbox, SandboxLimits

PROBE = r'''
import json, os, socket
r = {"uid": os.getuid(), "gid": os.getgid()}
for line in open("/proc/self/status"):
    k, _, v = line.partition(":")
    if k in ("CapEff", "CapBnd", "NoNewPrivs"):
        r[k] = v.strip()
try:
    open("/etc/failgate-probe", "w")
    r["rootfs_writable"] = True
except OSError:
    r["rootfs_writable"] = False
try:
    open("/workspace/.failgate-probe", "w").write("x")
    r["workspace_writable"] = True
except OSError:
    r["workspace_writable"] = False
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    r["network"] = True
except OSError:
    r["network"] = False
def cg(name):
    try:
        return open("/sys/fs/cgroup/" + name).read().strip()
    except OSError:
        return None
r["pids_max"], r["memory_max"], r["cpu_max"] = cg("pids.max"), cg("memory.max"), cg("cpu.max")
print("PROBE" + json.dumps(r))
'''


# 安装阶段的出网探针（ADR 0042）：和真实安装一样经 sandbox.install 起容器
EGRESS_PROBE = r'''
import json, socket, urllib.request
r = {}
def get(url):
    try:
        urllib.request.urlopen(url, timeout=15).read(64)
        return "ok"
    except Exception as e:
        return type(e).__name__ + ": " + str(e)[:120]
r["pypi"] = get("https://pypi.org/simple/pip/")
r["other_site"] = get("https://example.com/")
r["proxy_to_lan"] = get("https://192.168.1.1/")
for name, addr in [("direct_internet", ("1.1.1.1", 443)), ("direct_lan", ("192.168.1.1", 80))]:
    try:
        socket.create_connection(addr, timeout=4).close()
        r[name] = "connected"
    except OSError as e:
        r[name] = type(e).__name__ + ": " + str(e)[:80]
print("EGRESS" + json.dumps(r))
'''


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str


async def self_check(sandbox: DockerSandbox, image: str) -> list[CheckItem]:
    items: list[CheckItem] = []
    version = await sandbox.server_version()
    items.append(
        CheckItem("Docker 引擎", version is not None, version or "连不上（Docker 没启动？）")
    )
    if version is None:
        return items
    await sandbox.ensure_image(image)

    volume = await sandbox.create_workspace("selfcheck")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.to_thread(Path(tmp, "probe.py").write_text, PROBE, encoding="utf-8")
            await sandbox.copy_in(volume, Path(tmp), image)
        res = await sandbox.run(image, volume, ["python", "probe.py"], timeout_s=30)
        line = next((ln for ln in res.stdout.splitlines() if ln.startswith("PROBE")), None)
        if line is None:
            items.append(CheckItem("探针", False, res.output_tail(10)))
            return items
        p = json.loads(line[len("PROBE"):])
        lim = sandbox.limits
        mem_bytes = _parse_bytes(lim.memory)
        items += [
            CheckItem("非 root", p["uid"] == 1000, f"uid={p['uid']} gid={p['gid']}"),
            CheckItem(
                "去掉全部 capabilities",
                int(p.get("CapEff", "1"), 16) == 0 and int(p.get("CapBnd", "1"), 16) == 0,
                f"CapEff={p.get('CapEff')} CapBnd={p.get('CapBnd')}",
            ),
            CheckItem("禁止提权", p.get("NoNewPrivs") == "1", f"NoNewPrivs={p.get('NoNewPrivs')}"),
            CheckItem("根文件系统只读", not p["rootfs_writable"], "写 /etc 被拒绝"
                      if not p["rootfs_writable"] else "可以写 /etc"),
            CheckItem("工作区可写", p["workspace_writable"], "/workspace 属主是沙箱用户"),
            CheckItem(
                "运行阶段断网", not p["network"], "连不上外网" if not p["network"] else "能连外网"
            ),
            CheckItem(
                "进程数上限", p["pids_max"] == str(lim.pids_run), f"pids.max={p['pids_max']}"
            ),
            CheckItem(
                "内存上限", p["memory_max"] == str(mem_bytes), f"memory.max={p['memory_max']}"
            ),
            CheckItem(
                "CPU 上限",
                p["cpu_max"] == f"{int(lim.cpus * 100000)} 100000",
                f"cpu.max={p['cpu_max']}",
            ),
        ]

        res = await sandbox.run(
            image, volume, ["python", "-c", "import time; time.sleep(60)"], timeout_s=2
        )
        items.append(CheckItem(
            "超时终止", res.timed_out and res.duration_s < 20,
            f"exit={res.exit_code} 用时 {res.duration_s:.1f}s",
        ))

        small = DockerSandbox(sandbox.docker, limits=SandboxLimits(memory="128m"))
        res = await small.run(
            image, volume, ["python", "-c", "b = bytearray(512 * 1024 * 1024)"], timeout_s=30
        )
        items.append(CheckItem(
            "内存超限识别", res.oom_killed and not res.timed_out,
            f"exit={res.exit_code} OOM={res.oom_killed}（依据：{res.oom_source}）",
        ))
        items += await _egress_items(sandbox, image, volume)
    finally:
        await sandbox.remove_workspace(volume)
    return items


async def _egress_items(sandbox: DockerSandbox, image: str, volume: str) -> list[CheckItem]:
    if not sandbox.egress:
        return [CheckItem("安装阶段出网白名单", False,
                          f"SANDBOX_INSTALL_NETWORK={sandbox.install_network}：安装阶段可以访问整个"
                          "互联网（PR 的 setup.py 会在这里执行）；改成 egress 启用白名单代理")]
    with tempfile.TemporaryDirectory() as tmp:
        await asyncio.to_thread(Path(tmp, "egress_probe.py").write_text, EGRESS_PROBE,
                                encoding="utf-8")
        await sandbox.copy_in(volume, Path(tmp), image)
    res = await sandbox.install(image, volume, ["python", "egress_probe.py"], timeout_s=90,
                                allowed=(("python", "egress_probe.py"),))
    line = next((ln for ln in res.stdout.splitlines() if ln.startswith("EGRESS")), None)
    if line is None:
        return [CheckItem("安装阶段出网探针", False, res.output_tail(10))]
    p = json.loads(line[len("EGRESS"):])
    allow = ", ".join(sandbox.egress_allow)
    return [
        CheckItem("安装阶段能访问 PyPI", p["pypi"] == "ok",
                  f"经代理：{p['pypi'][:80]}（白名单 {allow}）"),
        CheckItem("安装阶段其他网站被拒", "403" in p["other_site"], p["other_site"][:80]),
        CheckItem("安装阶段不能绕过代理", p["direct_internet"] != "connected",
                  f"直连 1.1.1.1:443：{p['direct_internet'][:60]}"),
        CheckItem("安装阶段连不到局域网",
                  p["direct_lan"] != "connected" and "403" in p["proxy_to_lan"],
                  f"直连：{p['direct_lan'][:40]}；经代理：{p['proxy_to_lan'][:40]}"),
    ]


def _parse_bytes(size: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    s = size.strip().lower()
    return int(float(s[:-1]) * units[s[-1]]) if s[-1] in units else int(s)
