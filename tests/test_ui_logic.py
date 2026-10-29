"""Tests for UI logic that does not need a terminal: panes, key names, width helpers, keymap."""
import unittest

from cursegit.ui import keys
from cursegit.ui.draw import clip, display_width, fit, key_name, sanitize, skip_cells
from cursegit.ui.panes import ListPane, Row, TextPane


class Width(unittest.TestCase):
    def test_wide_chars(self):
        self.assertEqual(display_width("ab"), 2)
        self.assertEqual(display_width("日本"), 4)
        self.assertEqual(clip("日本語", 3), "日")
        self.assertEqual(fit("日", 4), "日  ")
        self.assertEqual(clip("abcdef", 4, ellipsis=True), "abc…")
        self.assertEqual(skip_cells("abcdef", 2), "cdef")

    def test_sanitize(self):
        self.assertEqual(sanitize("a\tb\x01\n"), "a    b?")


class KeyNames(unittest.TestCase):
    def test_names(self):
        self.assertEqual(key_name("\n"), "enter")
        self.assertEqual(key_name("\r"), "enter")
        self.assertEqual(key_name("\t"), "tab")
        self.assertEqual(key_name("\x1b"), "esc")
        self.assertEqual(key_name(" "), "space")
        self.assertEqual(key_name("\x13"), "ctrl-s")
        self.assertEqual(key_name("\x7f"), "backspace")
        self.assertEqual(key_name("q"), "q")
        self.assertEqual(key_name(None), "")


class Keymap(unittest.TestCase):
    def test_scope_precedence(self):
        self.assertEqual(keys.lookup("enter", "branches"), "switch")
        self.assertEqual(keys.lookup("enter", "status"), "open_detail")
        self.assertEqual(keys.lookup("D", "stash"), "stash_drop")
        self.assertEqual(keys.lookup("D", "status"), "discard_all")
        self.assertEqual(keys.lookup("q", "log"), "quit")
        self.assertIsNone(keys.lookup("~", "log"))

    def test_help_mentions_every_binding(self):
        text = "\n".join(keys.help_lines("status"))
        for b in keys.BINDINGS:
            if b.scope in ("status", "global"):
                self.assertIn(b.help, text)


class ListPaneTests(unittest.TestCase):
    def rows(self):
        return [
            Row("STAGED", selectable=False),
            Row("a", key="a"),
            Row("b", key="b"),
            Row("CHANGES", selectable=False),
            Row("c", key="c"),
        ]

    def test_skips_headers(self):
        p = ListPane()
        p.set_rows(self.rows())
        self.assertEqual(p.selected.key, "a")
        p.move(1)
        self.assertEqual(p.selected.key, "b")
        p.move(1)
        self.assertEqual(p.selected.key, "c")
        p.move(1)
        self.assertEqual(p.selected.key, "c")
        p.move(-5)
        self.assertEqual(p.selected.key, "a")
        p.end()
        self.assertEqual(p.selected.key, "c")
        p.home()
        self.assertEqual(p.selected.key, "a")

    def test_keeps_cursor_across_refresh(self):
        p = ListPane()
        p.set_rows(self.rows())
        p.move(1)  # on "b"
        p.set_rows([Row("H", selectable=False), Row("x", key="x"), Row("b", key="b")])
        self.assertEqual(p.selected.key, "b")
        # the item vanished: cursor stays at the same index, snapped to a selectable row
        p.set_rows([Row("H", selectable=False), Row("x", key="x")])
        self.assertEqual(p.selected.key, "x")
        p.set_rows([])
        self.assertIsNone(p.selected)


class TextPaneTests(unittest.TestCase):
    def test_scroll_bounds(self):
        t = TextPane()
        t.set_text("\n".join(str(i) for i in range(50)))
        t.scroll(100, 10)
        self.assertEqual(t.top, 40)
        t.scroll(-100, 10)
        self.assertEqual(t.top, 0)
        t.end(10)
        self.assertEqual(t.position(10), "41-50/50")
        t.set_text("short", keep_scroll=True)
        self.assertEqual(t.top, 0)


if __name__ == "__main__":
    unittest.main()
