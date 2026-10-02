"""回放开跑前的 Docker 预检：连不上就直接停，不花 Intake 的钱。"""

from __future__ import annotations

import pytest
import typer

from failgate import cli, home
from failgate.settings import Settings


def test_require_docker_stops_before_spending(monkeypatch):
    monkeypatch.setattr(home, "check_docker", lambda s, timeout=3.0: home.Check(
        "Docker", False, "引擎连不上", "Docker Desktop 可能没开"))
    with pytest.raises(typer.BadParameter, match="Docker 不可用：引擎连不上"):
        cli._require_docker(Settings())


def test_require_docker_passes_when_engine_answers(monkeypatch):
    monkeypatch.setattr(home, "check_docker", lambda s, timeout=3.0: home.Check(
        "Docker", True, "29.8.0"))
    cli._require_docker(Settings())
