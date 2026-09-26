from ordkit import chunks, unique


def test_unique_numbers() -> None:
    assert unique([1, 2, 2, 3, 1]) == [1, 2, 3]


def test_chunks() -> None:
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
