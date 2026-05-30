def selection_sort(values):
    """Sort a list using selection sort. Returns a new sorted list; input is unchanged."""
    result = list(values)
    n = len(result)
    for i in range(n):
        min_idx = i
        for j in range(i + 1, n):
            if result[j] < result[min_idx]:
                min_idx = j
        result[i], result[min_idx] = result[min_idx], result[i]
    return result
