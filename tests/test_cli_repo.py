"""`failgate repo repro`：没发布到 PyPI 的项目必须同时给源码仓库。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from failgate.cli import app
from failgate.repro.pypi import PackageNotFound, PyPIClient

REPO = "acme/app"


@pytest.fixture
def cli(monkeypatch, tmp_path) -> CliRunner:
    monkeypatch.chdir(tmp_path)  # 不读仓库里的 .env
    monkeypatch.delenv("WARDEN_DB_URL", raising=False)
    monkeypatch.setenv("FAILGATE_DB_URL", f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")

    async def unpublished(self: PyPIClient, name: str) -> dict:
        raise PackageNotFound(f"PyPI 上没有这个包：{name}")

    monkeypatch.setattr(PyPIClient, "releases", unpublished)
    # CI 上 rich 会上色并按终端宽度折行，报错信息会被截断；固定宽度、关掉颜色
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    return CliRunner()


def test_unpublished_package_needs_a_source_repo(cli: CliRunner):
    res = cli.invoke(app, ["repo", "repro", REPO, "--package", "acme-app"])
    assert res.exit_code != 0 and "--source" in res.output


def test_unpublished_package_with_source_goes_to_source_mode(cli: CliRunner):
    res = cli.invoke(app, ["repo", "repro", REPO, "--package", "acme-app",
                           "--import-name", "acme_app", "--source", REPO])
    assert res.exit_code == 0, res.output
    assert "没发布到 PyPI，所有 bug 都走 source 模式（L2）" in res.output
    res = cli.invoke(app, ["repo", "repro", REPO])  # 只查看：配置确实存下了
    assert "acme-app" in res.output and REPO in res.output
