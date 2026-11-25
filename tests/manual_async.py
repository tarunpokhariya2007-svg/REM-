"""Tiny deterministic coroutine driver for async primitives that need no OS loop."""
import asyncio


class _Future:
    def __init__(self):
        self._done = False
        self._callbacks = []
        self._result = None

    def __await__(self):
        yield self
        return self._result

    def done(self):
        return self._done

    def set_result(self, value):
        self._done = True
        self._result = value
        for callback in self._callbacks:
            callback(self)

    def add_done_callback(self, callback, *, context=None):
        self._callbacks.append(callback)

    def remove_done_callback(self, callback):
        self._callbacks = [one for one in self._callbacks if one != callback]
        return 0


class _Loop:
    def create_future(self):
        return _Future()


def run_ready(coroutine):
    """Run an immediate coroutine to completion, failing if it actually waits."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("coroutine yielded; use manual_loop for a blocking queue case")


class manual_loop:
    """Install a minimal running-loop context for asyncio.Queue's waiter futures."""
    def __enter__(self):
        self.previous = asyncio.events._get_running_loop()
        self.loop = _Loop()
        asyncio.events._set_running_loop(self.loop)
        return self.loop

    def __exit__(self, exc_type, exc, tb):
        asyncio.events._set_running_loop(self.previous)
