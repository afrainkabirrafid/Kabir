"""Self-hosting tests (Kabir 4.0.0 Wave 4, worker J): the Kabir lexer
written in Kabir itself (kabir/selfhost/lexer.kbr).

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
PKG = os.path.join(ROOT, "kabir")
LEXER_KBR = os.path.join(PKG, "selfhost", "lexer.kbr")


def _esc(text):
    """Mirror the esc() function in kabir/selfhost/lexer.kbr."""
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
    """Run kabir/selfhost/lexer.kbr over `path` via the VM; return the proc."""
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
                                "10 examples plus kabir/selfhost/lexer.kbr")
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


# ----------------------------------------------------------------------
# Self-hosted parser (Kabir 5.0.0 Wave 1, Leap 1).
#
# The Kabir parser (kabir/selfhost/parser.kbr) takes a token stream (the
# format kabir/selfhost/lexer.kbr prints, as JSON -- see kabir/selfhost.py)
# and prints the program's AST as one JSON object. The canonical
# form is defined in kabir/selfhost.py: every node is
# {"type", "line", "col", ...fields} in a fixed key order, so the
# Python parser's AST and the Kabir parser's AST must serialize
# to the identical JSON string. The comparison below is a plain
# string comparison -- any difference in node type, field, value,
# number type (2 vs 2.0), or position fails the test.
# ----------------------------------------------------------------------

from kabir.parser import parse as _py_parse
from kabir.selfhost import (ast_to_dict, canonical_ast_json,
                            kabir_ast_json, run_kabir_parser)


def check_parser_source(testcase, source):
    """Parse `source` with both parsers, assert identical AST JSON."""
    testcase.assertEqual(kabir_ast_json(source),
                         canonical_ast_json(source))


PARSER_SNIPPETS = [
    # Statements, assignment, separators.
    "let x = 5\n",
    "let mut xs = [1, 2]\nlet mut = 5\nprint(mut)\n",
    "let a = 1; let b = 2\nprint(a + b)\n",
    "arr[0] = 5\narr[0] += 2\nx += 3\nx -= 1\n",
    "{ let x = 2\n print(x) }\nprint({\"a\": 1})\n",
    "return\n",
    # Types, borrows, async.
    "let x: number = 5\n"
    "func add(a: number, b: number) -> number { return a + b }\n",
    "func f(x: &number) -> &mut number { return x }\n",
    "let y = &x\nlet z = &mut x\nlet w = &mut\n",
    "let mut x: &mut array = y\n",
    "async func producer(ch) { let x = await add(1, 2) }\n",
    "let g = async func(a) { return await a }\n",
    # Control flow.
    "if a { print(1) } elif b { print(2) } else { print(3) }\n",
    "if true {\n print(1)\n}\nelse {\n print(2)\n}\n",
    "while true { break }\nfor x in [1, 2] { print(x) }\n",
    "while i < 3 { if i == 1 { continue } i = i + 1 }\n",
    # Match: literals, bindings, guards, trailing comma, empty.
    "let m = match v { 1 => \"one\", n if n > 5 => \"big\", _ => \"other\" }\n",
    "let m = match v { \"a\" => 1, true => 2, null => 3, _ => 4 }\n",
    "let m = match v { 1 => \"one\", _ => \"other\", }\n",
    "let m = match v { }\n",
    "let x = match v { n => n + 1 }\n",
    # Functions, calls, methods, index chains.
    "func fib(n) { if n < 2 { return n } return fib(n - 1) + fib(n - 2) }\n",
    "func outer() { func inner() { return 1 } return inner() }\n",
    "let f = func(a, b) { return a + b }\n",
    "let x = spawn(f())\nlet y = spawn(f)\n",
    "\"abc\".upper()[0]\n\"abc\".upper().lower()\n",
    "let y = a.b.c[0](1, 2)\n",
    "print(\n  1,\n  2\n)\n",
    # Literals, strings, precedence.
    "let d = {\"a\": 1, \"b\": [1, 2]}\nlet e = {}\nlet f = []\n",
    "let d = {\"k\": func() { return 1 }}\n",
    "print(\"Hello, {name}! You are {age + 1}.\")\n",
    "print(\"a\\nb\\t\\\"c\\\\\")\n",
    "let x = -2 ** 2\nlet y = 2 ** 3 ** 2\nlet z = not true and false or true\n",
    "let x = 2.0\nlet y = 3.14\nlet z = 007\n",
    "let x = (1 + 2) * 3\nlet y = a and b or c and d\n",
    "let x = - -5\nlet y = not not true\n",
    "let x = 10 % 3\nlet y = 7 / 2\nlet x = a ** -2\n",
    "let s = \"\"\nlet t = \"{x}\"\n",
    "let x = 1 < 2 == true\n",
    "let s = \"x\"\nlet t = s[0]\n",
    # Imports: quoted file path and bare package name (7.0.0).
    "import \"lib.kbr\"\n",
    "import greet\n",
    "import my-pkg\n",
]


class TestSelfhostParser(unittest.TestCase):
    def test_parser_snippets(self):
        for source in PARSER_SNIPPETS:
            with self.subTest(source=source):
                check_parser_source(self, source)

    def test_canonical_form_keeps_number_types_distinct(self):
        # 2 (int) and 2.0 (float) must serialize differently, or a
        # parser that lost the distinction would still pass.
        int_json = canonical_ast_json("let x = 2\n")
        float_json = canonical_ast_json("let x = 2.0\n")
        self.assertIn('"value": 2}', int_json)
        self.assertIn('"value": 2.0}', float_json)
        self.assertNotEqual(int_json, float_json)
        check_parser_source(self, "let x = 2\n")
        check_parser_source(self, "let x = 2.0\n")

    def test_canonical_form_node_shape(self):
        node = ast_to_dict(_py_parse("let x = 1").statements[0])
        self.assertEqual(list(node.keys()),
                         ["type", "line", "col", "name", "value",
                          "type_ann", "mutable"])
        self.assertEqual(node["type"], "LetStmt")
        self.assertIsNone(node["type_ann"])
        self.assertIs(node["mutable"], False)

    def test_parser_errors_reported(self):
        cases = [
            ("let x = ", "expression was expected"),
            ("if true { print(1)", "close brace"),
            ("print(1", "close paren"),
            ('import "dir/{name}.kbr"', "plain string"),
            ('import "notes.txt"', ".kbr"),
            # `import some_var` used to be here, but bare-name imports
            # (7.0.0) made it legal syntax -- it now fails at run time
            # (resolution), not at parse time. A number is still a
            # parse error, and so is a name starting with "_".
            ("import 5", "file path"),
            ("import _foo", "Invalid package name"),
            ('match v { "a{b}": 1 }', "plain string"),
            ('{"a": 1, 2: 3}', "Dict keys must be strings"),
            ("x.y = 1", "Only a variable or an index"),
            ("let 5 = x", "variable name"),
            ("let x = [1, 2,]", "expression was expected"),
        ]
        for source, phrase in cases:
            with self.subTest(source=source):
                with self.assertRaises(KabirError):
                    _py_parse(source)
                proc = run_kabir_parser(source)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "", proc.stderr)
                self.assertTrue(proc.stdout.startswith("ERROR\t"),
                                proc.stdout)
                self.assertIn(phrase, proc.stdout)

    def test_parser_error_carries_line_number(self):
        proc = run_kabir_parser("let a = 1\nlet b = ")
        self.assertTrue(proc.stdout.startswith("ERROR\t"), proc.stdout)
        parts = proc.stdout.rstrip("\n").split("\t")
        self.assertEqual(parts[2], "2")

    def test_bootstrap_full_corpus(self):
        """The proof: every .kbr file in the repo parses identically
        under the Kabir parser and the Python parser -- including
        kabir/selfhost/parser.kbr parsing itself."""
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
        self.assertIn(os.path.join(PKG, "selfhost", "parser.kbr"), corpus)
        self.assertGreaterEqual(len(corpus), 13,
                                "corpus shrank -- expected at least the "
                                "11 examples plus lexer.kbr and parser.kbr")
        for path in corpus:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(file=os.path.relpath(path, ROOT)):
                self.assertEqual(kabir_ast_json(source, path),
                                 canonical_ast_json(source, path))


# ----------------------------------------------------------------------
# Self-hosted compiler (Kabir 5.0.0 Wave 1, Leap 1).
#
# kabir/selfhost/compiler.kbr is a self-contained front end + compiler: it
# embeds the Kabir lexer and parser and prints the main code dict
# as JSON. The comparison is a plain string comparison against
# compile_source() -- every instruction, constant, line, column,
# slot, and the nested function code dicts must agree exactly.
# ----------------------------------------------------------------------

from kabir.compile import compile_source
from kabir.selfhost import (canonical_bytecode_json, run_kabir_compiler,
                            run_kabir_compiler_on_source)

import json  # noqa: E402  (used by the compiler tests below)

COMPILER_SNIPPETS = [
    "let x = 1 + 2 * 3\nprint(x)\n",
    "func fib(n) {\n if n < 2 { return n }\n"
    " return fib(n - 1) + fib(n - 2)\n}\nprint(fib(10))\n",
    "let x = \"hi {1 + 2}\"\n",
    "let m = match v { 1 => \"one\", _ => \"other\" }\n",
    "let xs = [1, 2, 3]\nfor x in xs { print(x) }\n",
    "while true { break }\n",
    "let f = func(a, b) { return a + b }\nprint(f(2, 3))\n",
    "if a { print(1) } elif b { print(2) } else { print(3) }\n",
    "let d = {\"a\": 1}\nprint(d[\"a\"])\n",
    "let x = 1\nx += 2\narr[0] = 5\narr[0] += 1\n",
    "let x = 1 and 2 or 3\nlet y = not true\nlet z = -5\n",
    "func outer(x) { func inner(y) { return x + y } return inner(1) }\n"
    "print(outer(10))\n",
    "async func add(a, b) { return a + b }\n"
    "async func main() { let t = spawn(add(1, 2))\n"
    " let r = await t\n return r }\n",
    "let s = spawn(f)\n",
    "let x = &y\nlet z = &mut y\n",
    "let x: number = 5\nfunc f(a: &number) -> number { return a }\n",
    "import \"helpers.kbr\"\n",
    "let n = 0\nwhile n < 3 { n = n + 1\n"
    " if n == 2 { continue }\n print(n) }\n",
    "let m = match v { n if n > 0 => \"pos\", 0 => \"zero\", _ => \"neg\" }\n",
    "let x = 2.5 * 2.0\nlet y = 10 % 3\nlet z = 2 ** 10\n",
    "print(\"nested {f(1, {\"k\": 2})} end\")\n",
    "let f = func() { let x = 1\n { let y = 2\n print(x + y) } }\n",
    "let x = [1, 2]\nlet y = x[0] + x[1]\nprint(x.upper())\n",
]


class TestSelfhostCompiler(unittest.TestCase):
    def test_compiler_snippets(self):
        for source in COMPILER_SNIPPETS:
            with self.subTest(source=source):
                kabir_json, py_json = run_kabir_compiler_on_source(source)
                self.assertEqual(kabir_json, py_json)

    def test_compiler_errors_reported(self):
        cases = [
            ("break\n", "can only be used inside a loop"),
            ("continue\n", "can only be used inside a loop"),
            ("return 5\n", "can only be used inside a function"),
            ("func f() { break }\n", "can only be used inside a loop"),
            ("let x = await f()\n", "can only be used inside an 'async func'"),
            ("let x = \"{ 1 + }\"\n", None),
        ]
        for source, phrase in cases:
            with self.subTest(source=source):
                with self.assertRaises(KabirError):
                    compile_source(source)
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".kbr", delete=False,
                        encoding="utf-8") as handle:
                    handle.write(source)
                    path = handle.name
                try:
                    proc = run_kabir_compiler(path)
                finally:
                    os.unlink(path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "", proc.stderr)
                self.assertTrue(proc.stdout.startswith("ERROR\t"),
                                proc.stdout)
                if phrase is not None:
                    self.assertIn(phrase, proc.stdout)

    def test_compiler_examples(self):
        """Every example file compiles to identical bytecode under
        the Kabir compiler and the Python compiler."""
        examples = os.path.join(ROOT, "examples")
        paths = sorted(os.path.join(examples, name)
                       for name in os.listdir(examples)
                       if name.endswith(".kbr"))
        self.assertGreaterEqual(len(paths), 11)
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(file=os.path.relpath(path, ROOT)):
                proc = run_kabir_compiler(path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = proc.stdout
                if out.endswith("\n"):
                    out = out[:-1]
                self.assertEqual(out,
                                 canonical_bytecode_json(source, path))

    def test_compiler_selfhost_files(self):
        """The Kabir compiler compiles the self-hosted lexer and
        parser to identical bytecode -- and itself."""
        for name in ("lexer.kbr", "parser.kbr", "compiler.kbr"):
            path = os.path.join(PKG, "selfhost", name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(file=name):
                proc = run_kabir_compiler(path)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = proc.stdout
                if out.endswith("\n"):
                    out = out[:-1]
                self.assertEqual(out,
                                 canonical_bytecode_json(source, path))

    def test_kabir_compiled_bytecode_runs(self):
        """Bytecode from the Kabir compiler is not just textually
        equal -- it runs in the VM and prints the same output."""
        import io
        from contextlib import redirect_stdout
        from kabir.vm import VM
        source = ("let name = \"Rafid\"\n"
                  "print(\"Hello, {name}!\")\n"
                  "for i in range(3) { print(i * i) }\n")
        kabir_json, _py_json = run_kabir_compiler_on_source(source)
        code = json.loads(kabir_json)
        buf = io.StringIO()
        with redirect_stdout(buf):
            VM().run_code(code)
        self.assertEqual(buf.getvalue(),
                         "Hello, Rafid!\n0\n1\n4\n")
