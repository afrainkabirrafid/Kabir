"""Formatter tests: kabir fmt rules, idempotency, --check, and safety.

The formatter must never change what a program does, never drop a
comment or mangle a string, and never write a file it cannot parse.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.cli import main as cli_main
from kabir.errors import KabirError
from kabir.formatter import format_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def fmt(source):
    return format_source(source, "test.kbr")


class TestFormattingRules(unittest.TestCase):
    def test_spacing_around_operators_and_commas(self):
        self.assertEqual(fmt("let x=1+2*3\n"), "let x = 1 + 2 * 3\n")
        self.assertEqual(fmt("print(1,2,3)\n"), "print(1, 2, 3)\n")
        self.assertEqual(fmt("let x=a==b and c!=d\n"),
                         "let x = a == b and c != d\n")

    def test_indentation(self):
        self.assertEqual(
            fmt("func f() {\nif true {\nprint(1)\n}\n}\n"),
            "func f() {\n    if true {\n        print(1)\n    }\n}\n")

    def test_trailing_whitespace_and_final_newline(self):
        self.assertEqual(fmt("let x = 1   \nlet y = 2"), "let x = 1\nlet y = 2\n")
        self.assertEqual(fmt("let x = 1\n\n\n"), "let x = 1\n")

    def test_blank_line_around_top_level_functions(self):
        source = ("let a = 1\nfunc f() {\nreturn 1\n}\nlet b = 2\n"
                  "func g() {\nreturn 2\n}\nlet c = 3\n")
        expected = ("let a = 1\n\nfunc f() {\n    return 1\n}\n\nlet b = 2\n\n"
                    "func g() {\n    return 2\n}\n\nlet c = 3\n")
        self.assertEqual(fmt(source), expected)

    def test_no_blank_lines_inside_blocks(self):
        self.assertEqual(
            fmt("while true {\n\nprint(1)\n\n\nprint(2)\n}\n"),
            "while true {\n    print(1)\n    print(2)\n}\n")

    def test_if_elif_else_layout(self):
        source = ("if a {\nprint(1)\n} elif b {\nprint(2)\n} else {\n"
                  "print(3)\n}\n")
        expected = ("if a {\n    print(1)\n} elif b {\n    print(2)\n"
                    "} else {\n    print(3)\n}\n")
        self.assertEqual(fmt(source), expected)

    def test_for_and_assignment_operators(self):
        self.assertEqual(
            fmt("for i in range(3) {\nx+=i\n}\n"),
            "for i in range(3) {\n    x += i\n}\n")

    def test_precedence_parentheses_kept_where_needed(self):
        self.assertEqual(fmt("let x = (a + b) * c\n"),
                         "let x = (a + b) * c\n")
        self.assertEqual(fmt("let x = a + (b + c)\n"),
                         "let x = a + (b + c)\n")
        self.assertEqual(fmt("let x = a + b + c\n"), "let x = a + b + c\n")
        self.assertEqual(fmt("let x = a ** b ** c\n"),
                         "let x = a ** b ** c\n")
        self.assertEqual(fmt("let x = (a ** b) ** c\n"),
                         "let x = (a ** b) ** c\n")
        self.assertEqual(fmt("let x = -a ** 2\n"), "let x = -(a ** 2)\n")
        self.assertEqual(fmt("let x = not a and b\n"),
                         "let x = not a and b\n")
        self.assertEqual(fmt("let x = (a or b) and c\n"),
                         "let x = (a or b) and c\n")

    def test_strings_preserved_exactly(self):
        # A space after the closing quote is plain whitespace and goes
        # away; the string itself comes back character for character.
        source = 'let s = "a  b\\n\\t\\"q\\" \\\\" \n'
        expected = source.replace('" \n', '"\n')
        self.assertEqual(fmt(source), expected)
        self.assertEqual(fmt('let s = "a#b # not a comment"\n'),
                         'let s = "a#b # not a comment"\n')

    def test_interpolation_is_reformatted(self):
        self.assertEqual(fmt('print("{a+1} and {b  *  c}")\n'),
                         'print("{a + 1} and {b * c}")\n')
        self.assertEqual(fmt('print("{t[\"k\"]}")\n'), 'print("{t[\"k\"]}")\n')

    def test_comments_preserved(self):
        source = ("# header comment\n"
                  "let x = 1  # trailing comment\n"
                  "# standalone comment\n"
                  "let y = 2\n")
        expected = ("# header comment\n"
                    "let x = 1  # trailing comment\n"
                    "# standalone comment\n"
                    "let y = 2\n")
        self.assertEqual(fmt(source), expected)

    def test_comments_inside_blocks_and_on_braces(self):
        source = ("func f() {\n# inside comment\nprint(1)\n"
                  "}  # after func\n")
        expected = ("func f() {\n    # inside comment\n    print(1)\n"
                    "}  # after func\n")
        self.assertEqual(fmt(source), expected)

    def test_comment_after_if_block(self):
        source = "if x {\nprint(1)\n}  # done\nprint(2)\n"
        expected = "if x {\n    print(1)\n}  # done\nprint(2)\n"
        self.assertEqual(fmt(source), expected)

    def test_comment_after_last_statement(self):
        self.assertEqual(fmt("let x = 1\n# trailing file note\n"),
                         "let x = 1\n# trailing file note\n")

    def test_blank_line_after_comment_block_is_kept(self):
        source = ("# header comment\n# second line\n\nlet x = 1\n")
        self.assertEqual(fmt(source), source)
        # No blank in the source, none added.
        self.assertEqual(fmt("# note\nlet x = 1\n"), "# note\nlet x = 1\n")
        # A section comment mid-file keeps its blank line too.
        source = "let a = 1\n# section note\n\nlet b = 2\n"
        self.assertEqual(fmt(source), source)

    def test_long_array_breaks(self):
        long = "let xs = [" + ", ".join(str(i) for i in range(30)) + "]\n"
        out = fmt(long)
        self.assertTrue(out.startswith("let xs = [\n"))
        self.assertTrue(out.rstrip().endswith("]"))
        self.assertNotIn(",]", out)  # no trailing comma
        self.assertEqual(fmt(out), out)  # idempotent

    def test_long_dict_breaks(self):
        source = ("let d = {\"aaaaaaaaaaaaaaaa\": 1, \"bbbbbbbbbbbbbbbb\": 2, "
                  "\"cccccccccccccccc\": 3, \"dddddddddddddddd\": 4}\n")
        out = fmt(source)
        self.assertTrue(out.startswith("let d = {\n"))
        self.assertEqual(fmt(out), out)

    def test_empty_collections(self):
        self.assertEqual(fmt("let a = []\nlet d = {}\n"),
                         "let a = []\nlet d = {}\n")

    def test_anonymous_function(self):
        source = "let f = func(x) {\nreturn x * 2\n}\nprint(f(3))\n"
        expected = ("let f = func(x) {\n    return x * 2\n}\nprint(f(3))\n")
        self.assertEqual(fmt(source), expected)

    def test_empty_file_and_comment_only_file(self):
        self.assertEqual(fmt(""), "")
        self.assertEqual(fmt("# just a note\n"), "# just a note\n")
        self.assertEqual(fmt("\n\n"), "")

    def test_semicolons_as_separators(self):
        self.assertEqual(fmt("let a = 1; let b = 2\n"),
                         "let a = 1\nlet b = 2\n")


class TestIdempotency(unittest.TestCase):
    def test_format_twice_is_a_noop(self):
        cases = [
            "let x=1+2\n",
            "func f(a,b){if a>b{return a}else{return b}}\n",
            "let d={\"k\": [1,2,{\"x\":true}], \"j\": \"s\"}\n",
            "for i in range(10) {\nif i%2==0 {\nprint(i)\n}\n}\n",
            "# note\nlet x = \"a {1+2} b\"  # hi\n",
            "let f = func(x) {\nreturn x\n}\n",
            "let x = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]\n",
        ]
        for source in cases:
            once = fmt(source)
            twice = fmt(once)
            self.assertEqual(once, twice, "not idempotent: %r" % source)

    def test_examples_are_idempotent(self):
        for name in sorted(os.listdir(EXAMPLES)):
            with open(os.path.join(EXAMPLES, name), encoding="utf-8") as h:
                source = h.read()
            once = fmt(source)
            self.assertEqual(fmt(once), once, name)


class TestInvalidFiles(unittest.TestCase):
    def test_syntax_error_raises(self):
        with self.assertRaises(KabirError):
            fmt("let x = \n")
        with self.assertRaises(KabirError):
            fmt("func f( {\n")

    def test_cli_skips_bad_file_and_never_writes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.kbr")
            good = os.path.join(tmp, "good.kbr")
            with open(bad, "w", encoding="utf-8") as h:
                h.write("let x = \n")
            with open(good, "w", encoding="utf-8") as h:
                h.write("let y=1\n")
            code = cli_main(["fmt", bad, good])
            self.assertEqual(code, 1)
            with open(bad, encoding="utf-8") as h:
                self.assertEqual(h.read(), "let x = \n")  # untouched
            with open(good, encoding="utf-8") as h:
                self.assertEqual(h.read(), "let y = 1\n")  # formatted


class TestCheckMode(unittest.TestCase):
    def _write(self, tmp, name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as h:
            h.write(text)
        return path

    def test_check_reports_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            messy = self._write(tmp, "messy.kbr", "let x=1\n")
            clean = self._write(tmp, "clean.kbr", "let x = 1\n")
            code = cli_main(["fmt", "--check", messy, clean])
            self.assertEqual(code, 1)
            with open(messy, encoding="utf-8") as h:
                self.assertEqual(h.read(), "let x=1\n")  # not modified

    def test_check_clean_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self._write(tmp, "clean.kbr", "let x = 1\n")
            self.assertEqual(cli_main(["fmt", "--check", clean]), 0)

    def test_fmt_writes_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "a.kbr", "let x=1\n")
            self.assertEqual(cli_main(["fmt", path]), 0)
            with open(path, encoding="utf-8") as h:
                self.assertEqual(h.read(), "let x = 1\n")

    def test_fmt_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cli_main(["fmt", os.path.join(tmp, "nope.kbr")])
            self.assertEqual(code, 1)

    def test_fmt_needs_a_file(self):
        self.assertEqual(cli_main(["fmt"]), 2)


class TestMeaningPreserved(unittest.TestCase):
    STDIN = {
        "calculator.kbr": "2 + 3\n10 / 4\n7 * 6\nquit\n",
        "todo.kbr": ("add Buy milk\nadd Learn Kabir\nlist\n"
                     "done 1\nlist\nquit\n"),
        "guessing-game.kbr": "\n".join(str(n) for n in range(1, 101)) + "\n",
    }

    def _run(self, path, stdin_text):
        return subprocess.run(
            [sys.executable, "-m", "kabir", "run", path],
            input=stdin_text, capture_output=True, text=True,
            cwd=ROOT, timeout=60)

    def test_formatted_examples_print_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in sorted(os.listdir(EXAMPLES)):
                with open(os.path.join(EXAMPLES, name),
                          encoding="utf-8") as h:
                    source = h.read()
                formatted = fmt(source)
                fpath = os.path.join(tmp, name)
                with open(fpath, "w", encoding="utf-8") as h:
                    h.write(formatted)
                stdin_text = self.STDIN.get(name, "")
                before = self._run(os.path.join(EXAMPLES, name), stdin_text)
                after = self._run(fpath, stdin_text)
                self.assertEqual(before.returncode, 0, name + ": " + before.stderr)
                self.assertEqual(after.returncode, 0, name + ": " + after.stderr)
                if name == "guessing-game.kbr":
                    # Random secret: only the shape is comparable.
                    self.assertIn("You got it in", after.stdout)
                    self.assertIn("The number was", after.stdout)
                else:
                    self.assertEqual(before.stdout, after.stdout, name)


if __name__ == "__main__":
    unittest.main()
