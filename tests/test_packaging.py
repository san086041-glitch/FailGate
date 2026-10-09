"""发布包的成员（ADR 0047）：sdist 不能带私有文档，wheel 要带齐运行时文件，
PyPI 上的 README 没有相对链接。

直接调 hatchling 的构建器（离线、几秒），不走 `python -m build`。
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("hatchling")
pytest.importorskip("hatch_fancy_pypi_readme")

from hatchling.builders.sdist import SdistBuilder  # noqa: E402
from hatchling.builders.wheel import WheelBuilder  # noqa: E402

from failgate import __version__  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = re.compile(r"(^|/)(docs|eval|tests|fixtures|secrets|scripts|deploy|\.github)/|\.env$")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("dist")
    sdist = next(iter(SdistBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    wheel = next(iter(WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    return {"sdist": Path(sdist), "wheel": Path(wheel)}


def sdist_members(path: Path) -> list[str]:
    with tarfile.open(path) as tar:
        return [n.split("/", 1)[1] for n in tar.getnames() if "/" in n]


def test_sdist_is_a_whitelist(built: dict[str, Path]):
    names = sdist_members(built["sdist"])
    assert not [n for n in names if PRIVATE.search(n)]  # 没有 docs/adr、eval/reports……
    tops = {n.split("/", 1)[0] for n in names}
    assert tops <= {"failgate", "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml",
                    "PKG-INFO", ".gitignore"}
    assert "CHANGELOG.md" in tops and "failgate" in tops
    assert not [n for n in names if "__pycache__" in n]


def test_wheel_has_the_runtime_files(built: dict[str, Path]):
    names = zipfile.ZipFile(built["wheel"]).namelist()
    assert built["wheel"].name == f"failgate-{__version__}-py3-none-any.whl"
    assert "failgate/console/index.html" in names
    assert "failgate/migrations/script.py.mako" in names
    assert any(n.startswith("failgate/migrations/versions/") for n in names)
    assert any(n.startswith("failgate/prompts/") and n.endswith(".md") for n in names)
    assert not [n for n in names if PRIVATE.search(n) or "__pycache__" in n]
    entry = next(n for n in names if n.endswith("entry_points.txt"))
    assert "failgate = failgate.cli:app" in zipfile.ZipFile(built["wheel"]).read(entry).decode()


def test_pypi_readme_has_no_relative_links(built: dict[str, Path]):
    wheel = zipfile.ZipFile(built["wheel"])
    meta = wheel.read(next(n for n in wheel.namelist() if n.endswith("METADATA"))).decode()
    assert f"Version: {__version__}" in meta
    assert "Content-Type: text/markdown" in meta
    assert not re.findall(r'src="(?!https?://)[^"]+"', meta)
    assert not re.findall(r'href="(?!https?://|#|mailto:)[^"]+"', meta)
    assert not re.findall(r"\]\((?!https?://|#|mailto:)[^)]+\)", meta)
    assert "https://raw.githubusercontent.com/san086041-glitch/FailGate/main/docs/media/" in meta
