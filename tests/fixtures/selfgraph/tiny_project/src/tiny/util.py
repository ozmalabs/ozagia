"""Small pure helpers used by tiny.core."""


def clamp(value: int, low: int, high: int) -> int:
    """Clamp value to the inclusive low/high range."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))
