import pytest
from counter import lock, run_locked


def test_lock_is_released_after_an_error():
    with pytest.raises(ZeroDivisionError):
        run_locked(lambda: 1 // 0)
    assert not lock.locked()
