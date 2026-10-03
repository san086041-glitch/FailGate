"""安装阶段的出网白名单（ADR 0042）。

run 阶段一直断网；install 阶段要 pip install，原来用 bridge 网络、能访问整个互联网。
而 pip 会执行被测项目自己的构建代码（setup.py、构建后端）——核验 PR 时就是 PR 作者的
代码——它可以下载任意东西、扫局域网、把能读到的东西发出去。

现在 install 容器只接入一个 --internal 的 Docker 网络（本身不通外网），通过代理出去：

    install 容器 ──(failgate-egress，internal)──▶ egress-proxy ──(bridge)──▶ 互联网
                         没有别的出口                 tinyproxy：只放行白名单域名的 443

- 代理只按域名放行（默认 pypi.org、files.pythonhosted.org，加上配置的 PIP 索引的主机），
  其余一律 403；只允许 CONNECT 到 443。
- 局域网和宿主机：白名单域名的 DNS 由 PyPI 控制，攻击者没法让它们解析到局域网地址；
  直连（不走代理）在 internal 网络上本来就不通。
- 代理镜像是我们自己写的 Dockerfile（alpine + tinyproxy），约 10 MB；配置在启动时由
  环境变量生成，改白名单只要重建容器。

源码包在宿主机上下载（source.py），不经过这里。
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

NETWORK = "failgate-egress"
CONTAINER = "failgate-egress-proxy"
ALIAS = "egress-proxy"
PORT = 8888
IMAGE = "failgate-egress-proxy:1"
DEFAULT_ALLOW = ("pypi.org", "files.pythonhosted.org")
MODE = "egress"  # SANDBOX_INSTALL_NETWORK 取这个值时启用

_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")

# 启动脚本：把 ALLOW（逗号分隔的域名）写成 tinyproxy 的过滤规则（整段域名精确匹配）
_ENTRY = r"""#!/bin/sh
set -eu
: > /tmp/filter
for h in $(echo "$ALLOW" | tr ',' ' '); do
  printf '^%s$\n' "$(echo "$h" | sed 's/\./\\./g')" >> /tmp/filter
done
cat > /tmp/tinyproxy.conf <<EOF
Port 8888
Listen 0.0.0.0
Timeout 600
MaxClients 64
LogLevel Connect
DisableViaHeader Yes
Allow 10.0.0.0/8
Allow 172.16.0.0/12
Allow 192.168.0.0/16
ConnectPort 443
Filter "/tmp/filter"
FilterType ere
FilterURLs Off
FilterCaseSensitive Off
FilterDefaultDeny Yes
EOF
exec tinyproxy -d -c /tmp/tinyproxy.conf
"""

# 启动脚本用 base64 写进镜像：Dockerfile 的 RUN 一行一条指令，多行脚本直接写会被拆开
_ENTRY_B64 = base64.b64encode(_ENTRY.encode()).decode()
DOCKERFILE = f"""FROM alpine:3.20
RUN apk add --no-cache tinyproxy
RUN echo {_ENTRY_B64} | base64 -d > /entry.sh && chmod 755 /entry.sh
USER nobody
ENTRYPOINT ["/entry.sh"]
"""


def allow_list(extra: Sequence[str] = (), index_url: str = "") -> list[str]:
    """白名单：默认的 PyPI 两个域名 + 配置的额外域名 + PIP 索引的主机。只收合法域名。"""
    hosts = [*DEFAULT_ALLOW, *extra]
    if index_url:
        hosts.append(urlsplit(index_url).hostname or "")
    out: list[str] = []
    for h in hosts:
        h = h.strip().lower()
        if h and _HOST.fullmatch(h) and h not in out:
            out.append(h)
    return out


def proxy_env() -> list[str]:
    """install 容器的环境变量：pip（requests / urllib）都认大小写两种写法。"""
    url = f"http://{ALIAS}:{PORT}"
    return [f"HTTP_PROXY={url}", f"HTTPS_PROXY={url}", f"http_proxy={url}",
            f"https_proxy={url}", "NO_PROXY=", "no_proxy="]


def run_args(allow: Sequence[str]) -> list[str]:
    """代理容器的 docker run 参数（不含 docker 本身）。先接 bridge 出网，再接 internal 网络。"""
    return [
        "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
        "--label", "failgate.sandbox=egress",
        "--network", "bridge",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=8m",
        "--memory", "128m", "--pids-limit", "64",
        "--env", f"ALLOW={','.join(allow)}",
        IMAGE,
    ]
