import pytest
from pydantic import ValidationError

from failgate.repro.config import PackageConfig, ReproConfig, ReproMode, choose_mode
from failgate.repro.sandbox import DEFAULT_INSTALL_PREFIXES, SandboxError, check_command


def test_install_argv_rendering():
    cfg = PackageConfig(name="black")
    argv = cfg.install_argv("23.11.0")
    assert argv == ["pip", "install", "--no-cache-dir", "black==23.11.0"]
    check_command(argv, DEFAULT_INSTALL_PREFIXES)
    extras = PackageConfig(name="black", install="pip install {name}[d]=={version}")
    assert extras.install_argv("24.1.0")[-1] == "black[d]==24.1.0"


def test_install_template_cannot_escape_to_other_commands():
    # 模板渲染后按 argv 执行；非 pip install 的命令会被 install 阶段的白名单拒绝
    cfg = PackageConfig(name="black", install="sh -c 'pip install {name}=={version}'")
    with pytest.raises(SandboxError):
        check_command(cfg.install_argv("1.0"), DEFAULT_INSTALL_PREFIXES)


@pytest.mark.parametrize(
    "kw",
    [{"name": "black; rm -rf /"}, {"name": "black", "install": "pip install {name}=={oops}"}],
)
def test_invalid_config_rejected(kw):
    with pytest.raises(ValidationError):
        PackageConfig(**kw)


def test_import_name():
    assert PackageConfig(name="typing-extensions").module == "typing_extensions"
    assert PackageConfig(name="PyYAML", import_name="yaml").module == "yaml"


def test_choose_mode():
    pkg = ReproConfig(package=PackageConfig(name="black"))
    assert choose_mode(pkg, labels=["T: bug"], reported_version="23.1") == ReproMode.PACKAGE
    # 没有版本、没有配置包：只能分析
    assert choose_mode(pkg, labels=[], reported_version=None) == ReproMode.ANALYZE_ONLY
    assert choose_mode(ReproConfig(), labels=[], reported_version="1.0") == (
        ReproMode.ANALYZE_ONLY
    )
    # 特殊硬件：标签或正文关键词
    assert choose_mode(pkg, labels=["GPU"], reported_version="1.0") == ReproMode.ANALYZE_ONLY
    assert choose_mode(
        pkg, labels=[], reported_version="1.0", issue_text="fails on CUDA 12"
    ) == ReproMode.ANALYZE_ONLY
    # 显式指定模式
    assert choose_mode(
        ReproConfig(mode="package", package=PackageConfig(name="x")),
        labels=[], reported_version=None,
    ) == ReproMode.PACKAGE
