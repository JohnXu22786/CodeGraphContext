"""Regression tests for bounded, per-index path memoization."""

from codegraphcontext.tools.indexing.resolution.calls import (
    _RESOLVED_POSIX_CACHE_SIZE,
    _resolved_posix,
)


def test_resolved_path_cache_has_a_fixed_capacity(tmp_path):
    """A large CALLS pass must not retain one path entry per repository file."""
    _resolved_posix.cache_clear()
    for index in range(_RESOLVED_POSIX_CACHE_SIZE + 1):
        _resolved_posix(str(tmp_path / f"file_{index}.py"))

    cache_info = _resolved_posix.cache_info()
    assert cache_info.maxsize == _RESOLVED_POSIX_CACHE_SIZE
    assert cache_info.currsize == _RESOLVED_POSIX_CACHE_SIZE
