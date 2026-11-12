"""Self-hosting tests (Kabir 4.0.0 Wave 4, worker J): the Kabir lexer
written in Kabir itself (selfhost/lexer.kbr).

The Kabir lexer is run via the Python VM (`kabir run`, the VM path)
with the target file path on stdin, and its printed token stream is
compared line-for-line with the Python lexer's output for the same
file, using the exact format the .kbr program prints:
    KIND<TAB>VALUE<TAB>LINE<TAB>COL
Numbers print as int:<digits> / float:<digits>; strings print their
parts as lit:<text> / expr:<text> joined by "|", with backslash,
newline, tab, carriage return and "|" backslash-escaped.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.lexer import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXER_KBR = os.path.join(ROOT, "selfhost", "lexer.kbr")


def _esc(text):
    """Mirror the esc() function in selfhost/lexer.kbr."""
    return (text.replace("\\", "\\\\").replace("\n", "\\n")
                .replace("\t", "\\t").replace("\r", "\\r")
                .replace("|", "\\|"))


def _kabir_num(value):
    """Render a number the way lexer.kbr's num_text() does: ints as
    str, floats as repr (num_text prints an integral float as str + ".0",
    which is exactly repr for ordinary decimal floats)."""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def format_token(tok):
    """Format one Python lexer token exactly as lexer.kbr prints it."""
    if tok.kind == "NUMBER":
        tag = "int" if isinstance(tok.value, int) else "float"
        val = tag + ":" + _kabir_num(tok.value)
    elif tok.kind == "STRING":
        val = "|".join(("lit:" if kind == "lit" else "expr:") + _esc(text)
                       for kind, text in tok.value)
    elif tok.kind == "EOF":
        val = "null"
    else:
        val = _esc(str(tok.value))
    return "%s\t%s\t%d\t%d" % (tok.kind, val, tok.line, tok.col)


def expected_lines(source, filename="<test>"):
    return [format_token(t) for t in tokenize(source, filename)]


def run_kabir_lexer(path):
    """Run selfhost/lexer.kbr over `path` via the VM; return the proc."""
    return subprocess.run(
        [sys.executable, "-m", "kabir", "run", LEXER_KBR],
        input=path + "\n", capture_output=True, text=True, cwd=ROOT)


def kabir_lines(path):
    proc = run_kabir_lexer(path)
    assert proc.returncode == 0, "lexer.kbr crashed: %s" % proc.stderr
    assert proc.stderr == "", "lexer.kbr wrote to stderr: %s" % proc.stderr
    lines = proc.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def check_source(testcase, source):
    """Lex `source` with both lexers and assert identical streams."""
    with tempfile.NamedTemporaryFile("w", suffix=".kbr", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = handle.name
    try:
        testcase.assertEqual(kabir_lines(path), expected_lines(source, path))
    finally:
        os.unlink(path)


class TestSelfhostLexer(unittest.TestCase):
    def test_token_kinds_representative_program(self):
        source = (
            'let name = "Rafid"\n'
            "let scores = [10, 20, 30]\n"
            "func total(xs) {\n"
            "    let sum = 0\n"
            "    for x in xs {\n"
            "        sum = sum + x\n"
            "    }\n"
            "    return sum\n"
            "}\n"
            'if total(scores) >= 60 and name != "" {\n'
            '    print("pass: {total(scores)}")\n'
            "} else {\n"
            '    print("fail")\n'
            "}\n"
        )
        check_source(self, source)

    def test_string_escapes_and_interpolation(self):
        source = (
            r'"a\nb\"c\\d\te\rf"' + "\n"
            + '"Hello, {name}! You are {age + 1}."\n'
            + '"{x}"\n'
            + '"{  spaced  }"\n'
            + '""\n'
            + '"{ {"k": 1} }"\n'
            + '"{ "{" + "}" }"\n'
            + r'"{ "a\"b" }"' + "\n"
            + '"tab\there"\n'
        )
        check_source(self, source)

    def test_comments(self):
        source = (
            "# a whole-line comment\n"
            "let x = 1 # set x\n"
            'let s = "a#b" # the # inside a string is not a comment\n'
            "# trailing comment with no newline at end of file"
        )
        check_source(self, source)

    def test_all_operators(self):
        source = (
            "a ** b == c != d <= e >= f += g -= h % i\n"
            "j + k - l * m / n = o < p > q\n"
            "func f(x: number) -> number {\n"
            "    return x\n"
            "}\n"
            "let m = match v { 1 => \"one\", _ => \"other\" }\n"
            "x.y[0], z; w: v\n"
            "(a) [b] {c}\n"
        )
        check_source(self, source)

    def test_numbers(self):
        source = "42 3.14 2.0 0.5 100 007 3. 1.5.2 0.10 123456789\n"
        check_source(self, source)

    def test_bootstrap_full_corpus(self):
        """The proof: every .kbr file in the repo tokenizes identically
        under the Kabir lexer and the Python lexer."""
        corpus = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".")
                                 and d not in ("dist", "__pycache__",
                                               "node_modules"))
            for name in sorted(filenames):
                if name.endswith(".kbr"):
                    corpus.append(os.path.join(dirpath, name))
        self.assertIn(os.path.join(ROOT, "examples", "hello.kbr"), corpus)
        self.assertIn(LEXER_KBR, corpus)
        self.assertGreaterEqual(len(corpus), 11,
                                "corpus shrank -- expected at least the "
                                "10 examples plus selfhost/lexer.kbr")
        for path in corpus:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(file=os.path.relpath(path, ROOT)):
                self.assertEqual(kabir_lines(path),
                                 expected_lines(source, path))

    def test_lexer_errors_reported(self):
        cases = [
            ('"never closed', "Unterminated string"),
            ("'hello'", "double quotes"),
            (r'"bad \q escape"', "Unknown escape"),
            ('"empty {} here"', "Empty interpolation"),
            ('"open {x"', "Unterminated interpolation"),
            ("let x = @", "Unexpected character"),
        ]
        for source, phrase in cases:
            with self.subTest(source=source):
                with self.assertRaises(KabirError):
                    tokenize(source)
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".kbr", delete=False,
                        encoding="utf-8") as handle:
                    handle.write(source)
                    path = handle.name
                try:
                    proc = run_kabir_lexer(path)
                finally:
                    os.unlink(path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(proc.stdout.startswith("ERROR\t"),
                                proc.stdout)
                self.assertIn(phrase, proc.stdout)


if __name__ == "__main__":
    unittest.main()
