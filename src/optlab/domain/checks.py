"""Small run-time checks that also narrow types (``assert`` is not used in src/)."""


def need[T](value: T | None, what: str) -> T:
    """``value``, or ValueError naming ``what`` if it is None."""
    if value is None:
        raise ValueError(f"missing {what}")
    return value
