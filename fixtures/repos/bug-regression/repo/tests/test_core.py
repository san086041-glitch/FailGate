from slugkit import slugify


def test_simple_words() -> None:
    assert slugify("Hello World") == "hello-world"


def test_accents_are_dropped() -> None:
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_custom_separator() -> None:
    assert slugify("a b", sep="_") == "a_b"
