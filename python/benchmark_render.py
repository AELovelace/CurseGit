"""Repeatable CPU-only benchmark for clipped diff lines; no terminal or Git mutation."""
import time

from cursegit.ui.panes import TextPane


class NullWindow:
    def getmaxyx(self):
        return (40, 120)

    def addstr(self, *args):
        pass  # Measure rendering preparation without terminal transport or paint latency.


def main():
    pane = TextPane()
    pane.set_text("\n".join("+" + "x" * 10000 for _ in range(35)))
    start = time.perf_counter()
    for _ in range(20):
        pane.draw(NullWindow(), 0, 0, 35, 120, colorize=False)
    print(f"20 long-line diff frames: {time.perf_counter() - start:.4f}s")


if __name__ == "__main__":
    main()
