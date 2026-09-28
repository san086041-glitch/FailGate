from failgate.settings import Settings


def test_db_url_reads_new_and_legacy_env_names(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # 不读仓库里的 .env
    monkeypatch.delenv("FAILGATE_DB_URL", raising=False)
    monkeypatch.setenv("WARDEN_DB_URL", "sqlite+aiosqlite:///old.db")
    assert Settings().failgate_db_url == "sqlite+aiosqlite:///old.db"
    monkeypatch.setenv("FAILGATE_DB_URL", "sqlite+aiosqlite:///new.db")
    assert Settings().failgate_db_url == "sqlite+aiosqlite:///new.db"


def test_db_url_default_and_init_kwarg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAILGATE_DB_URL", raising=False)
    monkeypatch.delenv("WARDEN_DB_URL", raising=False)
    assert Settings().failgate_db_url.endswith("/failgate.db")
    assert Settings(failgate_db_url="sqlite:///x.db").failgate_db_url == "sqlite:///x.db"
