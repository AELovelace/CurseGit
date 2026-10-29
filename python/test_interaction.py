"""Mouse and responsiveness regressions without a real terminal or network."""
import curses
from concurrent.futures import Future
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cursegit.model import FileChange, RepoState, Section
from cursegit.ui.app import App
from cursegit.ui import draw, popups, welcome
from cursegit.ui.panes import Row, TextPane
from cursegit.ui.theme import init_theme
from tests.test_app_smoke import FakeWin, Harness, Mouse


class AppInteractionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.repo.status.return_value = RepoState()
        self.repo.remotes.return_value = []
        self.repo.diff.side_effect = lambda file: "diff:" + file.path
        with patch("cursegit.ui.app.github.load_auth", return_value=SimpleNamespace(signed_in=False)), \
                patch.object(App, "_apply_auth"), patch("cursegit.ui.app.Worker"):
            self.app = App(self.repo)
        self.app.worker.busy = None
        self.addCleanup(self.app._reads.shutdown, wait=True, cancel_futures=True)
        with patch("curses.has_colors", return_value=False):
            init_theme()
        self.app.list.set_rows([
            Row(name, key=name, data=FileChange(name, Section.UNSTAGED, "M"))
            for name in ("one", "two", "three")
        ])
        self.app.geom = dict(width=120, height=30, body_y=2, body_h=26,
                             split=True, list_w=48, tabs=[(1, 10, 0), (12, 24, 1)])

    def finish_preview(self):
        self.app._detail_due = 0  # Advance the debounce deterministically, without wall-clock timing.
        self.app._poll_reads()
        if self.app._detail_future:
            self.app._detail_future[-1].result(timeout=2)
        self.app._poll_reads()

    def mouse(self, x, y, buttons):
        with patch("cursegit.ui.app.read_mouse", return_value=(x, y, buttons)):
            self.app.on_mouse()

    def test_press_release_runs_button_once_and_hover_does_not_redraw(self):
        self.app.geom["actions"] = [(1, 8, 28, "stage")]
        with patch.object(self.app, "handle_action") as action:
            self.mouse(3, 28, curses.BUTTON1_PRESSED)
            self.mouse(3, 28, curses.BUTTON1_RELEASED)
        action.assert_called_once_with("stage")
        self.app.need_draw = False
        self.mouse(40, 10, curses.REPORT_MOUSE_POSITION)
        self.assertFalse(self.app.need_draw)

    def test_two_immediate_presses_activate_row_once(self):
        with patch.object(self.app, "handle_action") as action:
            self.mouse(3, 2, curses.BUTTON1_PRESSED)
            self.mouse(3, 2, curses.BUTTON1_RELEASED)
            self.mouse(3, 2, curses.BUTTON1_PRESSED)
        action.assert_called_once_with("open_detail")

    def test_right_click_targets_row_before_opening_actions(self):
        with patch.object(self.app, "act_action_menu") as menu:
            self.mouse(3, 3, curses.BUTTON3_PRESSED)
        self.assertEqual(self.app.list.selected.key, "two")
        menu.assert_called_once()

    def test_wheel_over_detail_does_not_change_file(self):
        self.app.text.set_text("\n".join(str(i) for i in range(100)))
        self.mouse(70, 5, draw.WHEEL_DOWN)
        self.assertEqual(self.app.text.top, 3)
        self.assertEqual(self.app.list.selected.key, "one")

    def test_quick_navigation_loads_only_final_selection(self):
        for index in range(3):
            self.app.list.select(index)
            self.app.update_detail()
        self.repo.diff.assert_not_called()
        self.finish_preview()
        self.repo.diff.assert_called_once()
        self.assertEqual(self.app.text.lines, ["diff:three"])

    def test_old_preview_cannot_overwrite_new_selection(self):
        self.app.update_detail()
        serial, epoch, key, _ = self.app._pending_detail
        old = Future()
        self.app._detail_future = (serial, epoch, key, old)
        self.app._pending_detail = None
        self.app.list.select(1)
        self.app.update_detail()
        old.set_result(("WRONG OLD FILE", "old"))
        self.finish_preview()
        self.assertEqual(self.app.text.lines, ["diff:two"])

    def test_backtracking_uses_cache_and_refresh_invalidates_it(self):
        self.app.update_detail()
        self.finish_preview()
        self.app.list.select(1)
        self.app.update_detail()
        self.finish_preview()
        self.app.list.select(0)
        self.app.update_detail()
        self.assertEqual(self.repo.diff.call_count, 2)
        self.assertEqual(self.app.text.lines, ["diff:one"])
        self.app.update_detail(force=True)
        self.finish_preview()
        self.assertEqual(self.repo.diff.call_count, 3)

    def test_slow_diff_does_not_block_navigation(self):
        entered, release = threading.Event(), threading.Event()
        def slow(file):
            entered.set()
            release.wait(2)
            return file.path
        self.repo.diff.side_effect = slow
        self.app.update_detail()
        self.app._detail_due = 0
        self.app._poll_reads()
        try:
            self.assertTrue(entered.wait(1))
            self.app._navigate("down")
            self.assertEqual(self.app.list.selected.key, "two")
            self.assertFalse(self.app._detail_future[-1].done())
        finally:
            release.set()

    def test_background_refresh_rejects_pre_mutation_state(self):
        old = Future()
        self.app._refresh_future = (self.app._epoch, 0, old)
        self.app._epoch += 1
        stale = RepoState(files=[FileChange("old", Section.UNSTAGED, "M")])
        old.set_result((stale, True, None, None))
        self.app._poll_reads()
        self.assertEqual(self.app.state.files, [])

    def test_background_refresh_coalesces_and_updates_state(self):
        new = RepoState(files=[FileChange("fresh", Section.UNSTAGED, "M")])
        self.repo.status.return_value = new
        self.app._request_refresh()
        first = self.app._refresh_future
        self.app._request_refresh()
        self.assertIs(self.app._refresh_future, first)
        first[-1].result(timeout=2)
        self.app._poll_reads()
        self.assertIs(self.app.state, new)
        self.repo.status.assert_called_once()


class MousePopupTests(unittest.TestCase):
    def setUp(self):
        with patch("curses.has_colors", return_value=False):
            init_theme()

    def popup(self, keys, callback):
        harness = Harness(keys)
        screen = FakeWin(harness, 30, 120)
        with patch("curses.newwin", side_effect=lambda h, w, y, x: FakeWin(harness, h, w)), \
                patch("curses.getmouse", side_effect=harness.getmouse), \
                patch("curses.curs_set"):
            return callback(screen)

    def test_picker_click_chooses_second_option(self):
        # 40x5 centered popup at (40,12); rows start at local y=1.
        result = self.popup([Mouse(43, 14, curses.BUTTON1_PRESSED)],
                            lambda scr: popups.pick(scr, "Choose", ["one", "two"]))
        self.assertEqual(result, 1)

    def test_destructive_confirm_requires_explicit_yes_click(self):
        # Enter must not accept a dangerous action; the explicit Yes label can.
        result = self.popup(["\n", Mouse(41, 15, curses.BUTTON1_PRESSED)],
                            lambda scr: popups.confirm(scr, "Delete", "Delete file?", danger=True))
        self.assertTrue(result)

    def test_editor_cancel_button(self):
        # An 84x4 centered single-line editor: Cancel is at local x=10, y=3.
        result = self.popup([Mouse(29, 16, curses.BUTTON1_PRESSED)],
                            lambda scr: popups.prompt(scr, "Name", "unchanged"))
        self.assertIsNone(result)

    def test_welcome_can_quit_with_a_click(self):
        harness = Harness([Mouse(33, 17, curses.BUTTON1_PRESSED)])
        screen = FakeWin(harness, 30, 120)
        with patch("curses.has_colors", return_value=False), patch("curses.curs_set"), \
                patch("curses.getmouse", side_effect=harness.getmouse), \
                patch("cursegit.ui.welcome.enable_mouse", return_value=True):
            # Compute the real menu position rather than relying on the target path's length.
            menu = welcome.ListPane()
            menu.set_rows([Row(label, data=action) for action, _, label in welcome.OPTIONS])
            x, y, _ = welcome._draw(screen, ".", menu, "", False)
            harness.keys = [Mouse(x + 2, y + 2, curses.BUTTON1_PRESSED)]
            self.assertIsNone(welcome.welcome(screen, "."))

    def test_mouse_enabled_without_motion_events_or_click_delay(self):
        with patch("curses.mousemask", return_value=(1, 0)) as mask, patch("curses.mouseinterval") as delay:
            self.assertTrue(draw.enable_mouse())
        mask.assert_called_once_with(curses.ALL_MOUSE_EVENTS)
        delay.assert_called_once_with(0)


class RenderingTests(unittest.TestCase):
    def test_long_line_rendering_visits_only_visible_prefix(self):
        screen = Mock()
        screen.getmaxyx.return_value = (30, 120)
        with patch("cursegit.ui.draw.cell_width", wraps=draw.cell_width) as widths:
            draw.put(screen, 2, 1, "x" * 1_000_000, width=80, ellipsis=True)
        self.assertLess(widths.call_count, 400)
        self.assertEqual(screen.addstr.call_args.args[2], "x" * 79 + "\u2026")

    def test_tabs_wide_unicode_and_control_characters_still_clip(self):
        screen = Mock()
        screen.getmaxyx.return_value = (30, 120)
        for source, width in (("\txyz", 1), ("\u65e5\u672c\u8a9e", 3), ("a\x01b", 2), ("e\u0301long", 3)):
            draw.put(screen, 1, 1, source, width=width, ellipsis=True)
            expected = draw.fit(draw.sanitize(source), width, ellipsis=True)
            self.assertEqual(screen.addstr.call_args.args[2], expected)


if __name__ == "__main__":
    unittest.main()
