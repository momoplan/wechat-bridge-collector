from __future__ import annotations

import threading
from collections.abc import Callable


class IndependentLoops:
    """Supervise two event loops without sharing their waits or network requests."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._errors: list[BaseException] = []
        self._error_lock = threading.Lock()

    def run(self, messages: Callable[[], int], contacts: Callable[[], int]) -> int:
        results: list[int] = []

        def worker(target: Callable[[], int]) -> None:
            try:
                results.append(target())
            except BaseException as exc:
                with self._error_lock:
                    self._errors.append(exc)
                self.stop.set()

        threads = [
            threading.Thread(target=worker, args=(messages,), name="wechat-messages"),
            threading.Thread(target=worker, args=(contacts,), name="wechat-contacts"),
        ]
        started: list[threading.Thread] = []
        try:
            for thread in threads:
                thread.start()
                started.append(thread)
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(timeout=0.1)
        finally:
            self.stop.set()
            # Keep the method server and state owner alive until in-flight HTTP
            # calls (which have a bounded timeout) finish. No orphan worker.
            for thread in started:
                thread.join()
        if self._errors:
            raise self._errors[0]
        return max(results, default=0)
