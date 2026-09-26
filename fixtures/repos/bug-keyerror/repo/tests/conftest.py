import pytest


@pytest.fixture
def server_config() -> str:
    return "[server]\nhost = example.org\nport = 8080\n"
