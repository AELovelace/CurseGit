"""A single background thread that runs git jobs serially and reports back through a queue.

curses is not thread-safe, so the worker never draws. The UI polls `poll()` from the main loop.
Jobs run one at a time because concurrent git operations on one repo fight over the index lock.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Done:
    name: str
    result: Any = None
    error: BaseException | None = None


class Worker:
    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple[str, Callable[[], Any]]]" = queue.Queue()
        self._done: "queue.Queue[Done]" = queue.Queue()
        self.current: str | None = None
        self._thread = threading.Thread(target=self._loop, name="cursegit-worker", daemon=True)
        self._thread.start()

    def submit(self, name: str, fn: Callable[[], Any]) -> None:
        self._jobs.put((name, fn))

    def poll(self) -> list[Done]:
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    @property
    def busy(self) -> str | None:
        """Name of the running job, or None when idle."""
        if self.current:
            return self.current
        if not self._jobs.empty():
            return "queued"
        return None

    def _loop(self) -> None:
        while True:
            name, fn = self._jobs.get()
            self.current = name
            try:
                self._done.put(Done(name, result=fn()))
            except BaseException as e:  # noqa: BLE001 - report everything to the UI
                self._done.put(Done(name, error=e))
            finally:
                self.current = None
