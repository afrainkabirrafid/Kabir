"""REPL history tests: up/down arrow recall across sessions.

The history wiring lives in kabir.cli as _setup_history/_save_history,
built on stdlib readline (guarded: without readline the REPL simply
runs without history). These tests drive those helpers directly, so
no TTY is needed.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import _save_history, _setup_history

try:
    import readline
    HAS_READLINE = True
except ImportError:
    HAS_READLINE = False


@unittest.skipUnless(HAS_READLINE, "readline is not available here")
class TestReplHistory(unittest.TestCase):
    def test_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history")
            readline.clear_history()
            self.assertTrue(_setup_history(path))
            readline.add_history("let x = 5")
            readline.add_history("x + 1")
            self.assertTrue(_save_history(path))
            # Simulate a fresh REPL: clear and reload from disk.
            readline.clear_history()
            self.assertEqual(readline.get_current_history_length(), 0)
            self.assertTrue(_setup_history(path))
            self.assertEqual(readline.get_current_history_length(), 2)
            self.assertEqual(readline.get_history_item(1), "let x = 5")
            self.assertEqual(readline.get_history_item(2), "x + 1")
            readline.clear_history()

    def test_missing_history_file_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does-not-exist-yet")
            readline.clear_history()
            self.assertTrue(_setup_history(path))
            readline.add_history("let y = 2")
            self.assertTrue(_save_history(path))
            self.assertTrue(os.path.exists(path))
            readline.clear_history()

    def test_unwritable_history_path_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A directory is not a writable history file.
            self.assertTrue(_setup_history(tmp))
            self.assertFalse(_save_history(tmp))


if __name__ == "__main__":
    unittest.main()
