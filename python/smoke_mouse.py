"""Windows console smoke test: inject real mouse records, switch tabs, scroll, and quit."""
import ctypes
from ctypes import wintypes
import curses
import os
import threading
import time

from cursegit.git import GitRepo
from cursegit.ui.app import App
from cursegit.ui import app as app_module
from cursegit.ui.draw import WHEEL_DOWN


class Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class MouseRecord(ctypes.Structure):
    _fields_ = [("position", Coord), ("buttons", wintypes.DWORD),
                ("controls", wintypes.DWORD), ("flags", wintypes.DWORD)]


class KeyRecord(ctypes.Structure):
    _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                ("virtual", wintypes.WORD), ("scan", wintypes.WORD),
                ("char", wintypes.WCHAR), ("controls", wintypes.DWORD)]


class Event(ctypes.Union):
    _fields_ = [("mouse", MouseRecord), ("key", KeyRecord)]


class InputRecord(ctypes.Structure):
    _fields_ = [("kind", wintypes.WORD), ("event", Event)]


def main():
    if os.name != "nt":
        raise SystemExit("This console-input test requires Windows.")
    repo = GitRepo.discover(os.getcwd())
    if repo is None:
        raise SystemExit("Run in an existing repository; this check never creates or changes one.")
    application = App(repo, refresh_interval=0)
    observed, errors, mouse_events = [], [], []
    kernel = ctypes.windll.kernel32
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(InputRecord),
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.GetStdHandle(-10)

    def send(record):
        count = wintypes.DWORD()
        if not kernel.WriteConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(count)):
            raise ctypes.WinError()
        if count.value != 1:
            raise RuntimeError("Console did not accept the test input.")

    def mouse(x, y, buttons, flags=0):
        record = InputRecord()
        record.kind = 2
        record.event.mouse = MouseRecord(Coord(x, y), buttons, 0, flags)
        send(record)

    def inject():
        try:
            for x in (15, 3):  # Branches, then Status; neither click mutates Git state.
                mouse(x, 1, 1)
                time.sleep(0.08)
                mouse(x, 1, 0)
                time.sleep(0.25)
            mouse(80, 5, 0xFF880000, 4)  # One real Windows wheel-down record over the detail pane.
            time.sleep(0.2)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            record = InputRecord()
            record.kind = 1
            record.event.key = KeyRecord(True, 1, ord("Q"), 0, "q", 0)
            send(record)

    draw = application.draw
    on_mouse = application.on_mouse
    started = False

    def draw_and_start():
        nonlocal started
        draw()
        if not started:
            started = True
            threading.Thread(target=inject, daemon=True).start()  # Inject only after valid hit-test geometry exists.

    def record_mouse():
        on_mouse()
        observed.append(application.tab)

    application.draw = draw_and_start
    application.on_mouse = record_mouse
    original_read_mouse = app_module.read_mouse

    def capture_input():
        event = original_read_mouse()
        if event is not None:
            mouse_events.append(event)
        return event

    app_module.read_mouse = capture_input
    try:
        curses.wrapper(application.run)
    finally:
        app_module.read_mouse = original_read_mouse
    assert any(buttons & WHEEL_DOWN for _, _, buttons in mouse_events), mouse_events
    assert not errors, errors
    assert 1 in observed and application.tab == 0, observed
    print("PASS: real Windows mouse input switched Branches -> Status, received wheel input, and quit.")


if __name__ == "__main__":
    main()
