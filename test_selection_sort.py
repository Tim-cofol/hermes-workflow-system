import pytest
from selection_sort import selection_sort


@pytest.mark.parametrize("input_list, expected", [
    ([3, 1, 2], [1, 2, 3]),                        # basic unsorted list
    ([1, 2, 3], [1, 2, 3]),                        # already sorted
    ([3, 2, 1], [1, 2, 3]),                        # reverse sorted
    ([], []),                                       # empty list
    ([42], [42]),                                   # single element
    ([5, 5, 3, 3, 1], [1, 3, 3, 5, 5]),            # duplicates
])
def test_selection_sort_parametrized(input_list, expected):
    assert selection_sort(input_list) == expected


def test_selection_sort_does_not_mutate_input():
    original = [3, 1, 4, 1, 5, 9, 2, 6]
    snapshot = list(original)
    selection_sort(original)
    assert original == snapshot, "selection_sort must not modify the input list"
