from datetime import UTC, datetime

import httpx
import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from failgate.repro.pypi import PyPIClient, PyPIError, Release, normalize_version, pick_python


@pytest.mark.parametrize(
    ("raw", "package", "want"),
    [
        ("black 23.1.0", "black", "23.1.0"),
        ("v23.1", None, "23.1"),
        ("23.1.0 (compiled: yes)", None, "23.1.0"),
        ("On Black 23.11.0:", "black", "23.11.0"),
        ("black version: 22.3.0", "black", "22.3.0"),
        ("22.12b0", None, "22.12b0"),
        # 先出现的 Python 版本不能被当成包版本
        ("Python 3.12, black 23.1", "black", "23.1"),
        ("python 3.11.4 and 1.2.0", None, "1.2.0"),
        # 连字符 / 下划线 / 点在包名里等价
        ("scikit_learn==1.3.0", "scikit-learn", "1.3.0"),
    ],
)
def test_normalize_version(raw, package, want):
    assert normalize_version(raw, package) == Version(want)


@pytest.mark.parametrize(
    "raw", [None, "", "latest", "black, 24.4.3.dev27+g7fa1faf (compiled: no)", "1.0+local"]
)
def test_unreleased_or_missing_versions_are_rejected(raw):
    # 开发版、本地版本不是 PyPI 上的发布包，装不到
    assert normalize_version(raw, "black") is None


def _file(ts: str, *, requires: str | None = None, yanked: bool = False) -> dict:
    return {"upload_time_iso_8601": ts, "requires_python": requires, "yanked": yanked}


PYPI_JSON = {
    "releases": {
        "23.1.0": [_file("2023-02-01T00:00:00Z", requires=">=3.7")],
        "23.11.0": [_file("2023-11-10T00:00:00Z", requires=">=3.8")],
        "24.1.0": [_file("2024-01-26T00:00:00Z", requires=">=3.8")],
        "24.2.0": [_file("2024-02-12T00:00:00Z", yanked=True)],  # 撤回的不算最新
        "25.1a1": [_file("2025-01-01T00:00:00Z")],  # 预发布不算最新
        "0.1": [],  # 没有文件的发布，装不了
        "not-a-version": [_file("2020-01-01T00:00:00Z")],
    }
}


def client_with(payload: dict | None, calls: list[str] | None = None) -> PyPIClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(req.url))
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return PyPIClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_releases_parsing_and_cache():
    calls: list[str] = []
    pypi = client_with(PYPI_JSON, calls)
    rel = await pypi.releases("Black")
    assert Version("0.1") not in rel and len(rel) == 5
    r = rel[Version("23.11.0")]
    assert r.uploaded == datetime(2023, 11, 10, tzinfo=UTC)
    assert r.requires_python == SpecifierSet(">=3.8") and not r.yanked
    assert rel[Version("24.2.0")].yanked
    await pypi.releases("black")
    # 包名规范化后命中缓存，只请求一次
    assert calls == ["https://pypi.org/pypi/black/json"]


async def test_resolve_exact_and_latest():
    pypi = client_with(PYPI_JSON)
    r = await pypi.resolve("black", "black 23.11")
    assert r.version == Version("23.11.0")  # 23.11 == 23.11.0
    assert r.latest == Version("24.1.0")  # 不含撤回的 24.2.0 和预发布的 25.1a1


@pytest.mark.parametrize(
    ("name", "raw", "msg"),
    [
        ("black", "99.0", "没有 black==99.0"),
        ("black", "dev build", "无法"),
        ("bad name!", "1.0", "包名不合法"),
    ],
)
async def test_resolve_errors(name, raw, msg):
    with pytest.raises(PyPIError, match=msg):
        await client_with(PYPI_JSON).resolve(name, raw)


@pytest.mark.parametrize("raw", ["main@e079b7e", "24.1.1.dev28+g314f8cf", None])
async def test_unreleased_version_falls_back_to_last_release_before_issue(raw):
    # issue 创建于 2024-02-01（不带时区，和 SQLite 取出来的一样）：之前最新的正式版是 24.1.0
    r = await client_with(PYPI_JSON).resolve(
        "black", raw, fallback_before=datetime(2024, 2, 1)
    )
    assert r.version == Version("24.1.0") and r.substituted_for == (raw or "（未报告版本）")
    # 能装的版本不走降级
    exact = await client_with(PYPI_JSON).resolve(
        "black", "23.1.0", fallback_before=datetime(2024, 2, 1)
    )
    assert exact.version == Version("23.1.0") and exact.substituted_for is None
    with pytest.raises(PyPIError, match="前也没有正式版"):
        await client_with(PYPI_JSON).resolve("black", raw, fallback_before=datetime(2020, 1, 1))


async def test_unknown_package():
    with pytest.raises(PyPIError, match="没有这个包"):
        await client_with(None).resolve("nope", "1.0")


def rel(uploaded: str | None, requires: str | None = None) -> Release:
    return Release(
        version=Version("1.0"),
        uploaded=datetime.fromisoformat(uploaded) if uploaded else None,
        requires_python=SpecifierSet(requires) if requires else None,
        yanked=False,
    )


def test_pick_python_prefers_reported_then_configured():
    r = rel("2023-11-10T00:00:00+00:00", ">=3.8")
    assert pick_python(r, reported="Python 3.10.12") == "3.10"
    assert pick_python(r, reported=None, preferred="3.11") == "3.11"
    # 报告的版本不满足 requires_python（3.7），往下走
    assert pick_python(r, reported="3.7.2", preferred="3.9") == "3.9"


def test_pick_python_by_release_date():
    # 没有报告：选发布当时已经存在的最新 Python
    assert pick_python(rel("2023-11-10T00:00:00+00:00")) == "3.12"
    assert pick_python(rel("2022-01-15T00:00:00+00:00")) == "3.10"
    # 比所有支持的 Python 都早：用支持范围里最老的
    assert pick_python(rel("2018-01-01T00:00:00+00:00")) == "3.8"


def test_pick_python_respects_requires_python():
    assert pick_python(rel("2019-12-01T00:00:00+00:00", ">=3.10")) == "3.10"
    with pytest.raises(PyPIError, match="没有合适的镜像"):
        pick_python(rel(None, "<3.6"))
