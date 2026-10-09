from other_pkg import hosts


def test_hosts() -> None:
    assert hosts("[a]\nhost = x\n") == ["x"]
