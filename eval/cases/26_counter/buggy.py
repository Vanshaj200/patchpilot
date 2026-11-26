import threading
from collections.abc import Callable

lock = threading.Lock()


def run_locked(action: Callable[[], int]) -> int:
    """Run `action` while holding the lock."""
    lock.acquire()
    result = action()
    lock.release()
    return result
