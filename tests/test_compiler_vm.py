"""Compiler + VM tests (Kabir 2.0.0).

VM correctness: every example's stdout identical between the VM and
interpreter paths, recursion, closures, imports, string interpolation,
and/or short-circuiting, break/continue, for over string/dict, match,
annotated code, bytecode round-trip, compile-time errors, and the CLI
--interp flag / .kbc execution.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import builtins as _builtins

from kabir.compile import (compile_program, compile_source, load_bytecode,
                           save_bytecode)
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse
from kabir.semantics import binary_op, is_truthy, KabirRuntimeError
from kabir.vm import VM

PYTHON = "python3"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def run_vm(source, inputs=None, filename="<test>", seed=None):
    """Run source in the VM; return printed output."""
    vm = VM(source, filename=filename)
    if seed is not None:
        vm._random.seed(seed)
    buf = io.StringIO()
    if inputs is not None:
        it = iter(inputs)
        orig = _builtins.input
        _builtins.input = lambda prompt="": next(it)
        try:
            with redirect_stdout(buf):
                vm.run_source(source, filename=filename)
        finally:
            _builtins.input = orig
    else:
        with redirect_stdout(buf):
            vm.run_source(source, filename=filename)
    return buf.getvalue()


def run_interp(source, inputs=None, filename="<test>", seed=None):
    """Run source in the legacy interpreter; return printed output."""
    interp = Interpreter(source, filename=filename)
    if seed is not None:
        interp._random.seed(seed)
    buf = io.StringIO()
    if inputs is not None:
        it = iter(inputs)
        orig = _builtins.input
        _builtins.input = lambda prompt="": next(it)
        try:
            with redirect_stdout(buf):
                interp.run_source(source, filename=filename)
        finally:
            _builtins.input = orig
    else:
        with redirect_stdout(buf):
            interp.run_source(source, filename=filename)
    return buf.getvalue()


def run_both(source, inputs=None, seed=None):
    """Run source on both paths; return (vm_output, interp_output)."""
    return (run_vm(source, inputs, seed=seed),
            run_interp(source, inputs, seed=seed))


def run_vm_error(source, filename="<test>"):
    vm = VM(source, filename=filename)
    try:
        vm.run_source(source, filename=filename)
    except KabirError as err:
        return err
    return None


def run_vm_file(path, inputs=None, seed=None):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return run_vm(source, inputs, filename=path, seed=seed)


def run_interp_file(path, inputs=None, seed=None):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return run_interp(source, inputs, filename=path, seed=seed)


class TestSemantics(unittest.TestCase):
    def test_is_truthy(self):
        for falsy in (None, False, 0, 0.0, "", [], {}):
            self.assertFalse(is_truthy(falsy), falsy)
        for truthy in (True, 1, -1, 0.5, "x", [0], {"a": 1}):
            self.assertTrue(is_truthy(truthy), truthy)

    def test_int_division_rule(self):
        self.assertEqual(binary_op("/", 6, 3), 2)
        self.assertIsInstance(binary_op("/", 6, 3), int)
        self.assertEqual(binary_op("/", 5, 2), 2.5)
        self.assertIsInstance(binary_op("/", 5, 2), float)

    def test_binary_op_errors_match_interpreter(self):
        with self.assertRaises(KabirRuntimeError) as ctx:
            binary_op("/", 1, 0)
        self.assertIn("Division by zero", str(ctx.exception))
        with self.assertRaises(KabirRuntimeError) as ctx:
            binary_op("+", 1, "a")
        self.assertIn("Cannot add number and string", str(ctx.exception))
        with self.assertRaises(KabirRuntimeError) as ctx:
            binary_op("**", 1e308, 2)
        self.assertIn("too large", str(ctx.exception))


class TestVmBasics(unittest.TestCase):
    def test_arithmetic_and_comparison(self):
        vm_out, interp_out = run_both(
            "print(2 + 3 * 4)\nprint(10 / 4)\nprint(6 / 3)\n"
            "print(2 ** 10)\nprint(10 % 3)\nprint(1 < 2)\n"
            "print(\"abc\" < \"abd\")")
        self.assertEqual(vm_out, "14\n2.5\n2\n1024\n1\ntrue\ntrue\n")
        self.assertEqual(vm_out, interp_out)

    def test_error_messages_match_interpreter(self):
        cases = [
            ("print(1 / 0)", "Division by zero"),
            ("print(1 + \"a\")", "Cannot add number and string"),
            ("print(undefined_var)", "Undefined variable 'undefined_var'"),
            ("let x = 1\nx = 2\nprint(y)", "Undefined variable 'y'"),
            ("let a = [1]\nprint(a[5])", "Array index 5 is out of range"),
            ("let d = {\"a\": 1}\nprint(d[\"b\"])", "No key 'b' in this dict"),
            ("print(len(1))", "len() works on a string, array or dict"),
            ("let s = \"hi\"\nprint(s.nope())", "No method '.nope' on a string"),
            ("let d = {\"a\": 1}\nprint(d.name)", "Cannot use .name on a dict"),
            ("print(1 + true)", "Cannot add number and boolean"),
            ("print(-\"a\")", "Cannot negate string"),
            ("for x in 5 { print(x) }", "Cannot loop over number"),
        ]
        for source, fragment in cases:
            with self.subTest(source=source):
                vm_err = run_vm_error(source)
                self.assertIsNotNone(vm_err, source)
                self.assertIn(fragment, str(vm_err))
                interp = Interpreter(source, filename="<test>")
                with self.assertRaises(KabirError) as ctx:
                    interp.run_source(source, filename="<test>")
                self.assertIn(fragment, str(ctx.exception))

    def test_recursion(self):
        vm_out, interp_out = run_both(
            "func fib(n) { if n < 2 { return n } "
            "return fib(n - 1) + fib(n - 2) }\nprint(fib(15))")
        self.assertEqual(vm_out, "610\n")
        self.assertEqual(vm_out, interp_out)

    def test_recursion_limit_message(self):
        err = run_vm_error("func f(n) { return f(n + 1) }\nprint(f(0))")
        self.assertIsNotNone(err)
        self.assertIn("Too many nested function calls", str(err))

    def test_closures(self):
        vm_out, interp_out = run_both(
            "func make_adder(n) { return func(x) { return x + n } }\n"
            "let add5 = make_adder(5)\nprint(add5(10))\nprint(add5(1))")
        self.assertEqual(vm_out, "15\n6\n")
        self.assertEqual(vm_out, interp_out)

    def test_closure_captures_defining_scope(self):
        vm_out, interp_out = run_both(
            "let x = 1\nfunc get_x() { return x }\n"
            "let x = 99\nprint(get_x())\nprint(x)")
        # The top-level re-`let` redefines the same (global) binding,
        # so both paths must agree; the import test covers isolation.
        self.assertEqual(vm_out, interp_out)

    def test_recursive_anonymous_function_via_let(self):
        vm_out, interp_out = run_both(
            "let fact = func(n) { if n < 2 { return 1 } "
            "return n * fact(n - 1) }\nprint(fact(5))")
        self.assertEqual(vm_out, "120\n")
        self.assertEqual(vm_out, interp_out)

    def test_scoping_let_does_not_leak_from_block(self):
        err = run_vm_error("if true { let hidden = 1 }\nprint(hidden)")
        self.assertIsNotNone(err)
        self.assertIn("Undefined variable 'hidden'", str(err))
        vm_out, interp_out = run_both(
            "let x = 1\nif true { let x = 2\nprint(x) }\nprint(x)")
        self.assertEqual(vm_out, "2\n1\n")
        self.assertEqual(vm_out, interp_out)

    def test_string_interpolation(self):
        vm_out, interp_out = run_both(
            "let name = \"Kat\"\nlet x = 3\n"
            "print(\"Hello, {name}!\")\nprint(\"x + 1 = {x + 1}\")\n"
            "print(\"{[1, 2]}\")\nprint(\"plain, no holes\")")
        self.assertEqual(vm_out, "Hello, Kat!\nx + 1 = 4\n[1, 2]\n"
                         "plain, no holes\n")
        self.assertEqual(vm_out, interp_out)

    def test_string_methods(self):
        vm_out, interp_out = run_both(
            "print(\"  Hi There \".strip().lower())\n"
            "print(\"a,b,c\".split(\",\"))\n"
            "print(\"-\".join([\"a\", \"b\"]))\n"
            "print(\"hello\".contains(\"ell\"))\n"
            "print(\"hello\".replace(\"l\", \"L\"))")
        self.assertEqual(vm_out, "hi there\n[a, b, c]\na-b\ntrue\nheLLo\n")
        self.assertEqual(vm_out, interp_out)

    def test_compound_and_index_assignment(self):
        vm_out, interp_out = run_both(
            "let a = [1, 2, 3]\na[0] = 9\na[1] += 10\na[-1] -= 1\n"
            "let d = {\"x\": 1}\nd[\"x\"] += 5\nprint(a)\nprint(d)")
        self.assertEqual(vm_out, "[9, 12, 2]\n{\"x\": 6}\n")
        self.assertEqual(vm_out, interp_out)

    def test_negative_indices(self):
        vm_out, interp_out = run_both(
            "let a = [10, 20, 30]\nprint(a[-1])\nprint(a[-3])\n"
            "print(\"hello\"[-1])")
        self.assertEqual(vm_out, "30\n10\no\n")
        self.assertEqual(vm_out, interp_out)

    def test_for_over_string_and_dict(self):
        vm_out, interp_out = run_both(
            "for ch in \"ab\" { print(ch) }\n"
            "let d = {\"a\": 1, \"b\": 2}\nfor k in d { print(k) }")
        self.assertEqual(vm_out, "a\nb\na\nb\n")
        self.assertEqual(vm_out, interp_out)

    def test_break_and_continue(self):
        vm_out, interp_out = run_both(
            "for i in range(10) { if i == 3 { break } print(i) }\n"
            "for i in range(5) { if i == 2 { continue } print(i) }\n"
            "let i = 0\nwhile i < 5 { i += 1\n"
            " if i == 2 { continue }\n if i == 4 { break }\n print(i) }")
        self.assertEqual(vm_out, "0\n1\n2\n0\n1\n3\n4\n1\n3\n")
        self.assertEqual(vm_out, interp_out)

    def test_break_inside_nested_block_in_loop(self):
        vm_out, interp_out = run_both(
            "for i in range(5) { if true { if i == 2 { break } }\n"
            "print(i) }")
        self.assertEqual(vm_out, "0\n1\n")
        self.assertEqual(vm_out, interp_out)

    def test_while_loop_with_nested_for(self):
        vm_out, interp_out = run_both(
            "let i = 0\nwhile i < 3 { for j in range(2) { print(i * 10 + j) }\n"
            "i += 1 }")
        self.assertEqual(vm_out, "0\n1\n10\n11\n20\n21\n")
        self.assertEqual(vm_out, interp_out)

    def test_and_or_short_circuit_side_effects(self):
        source = (
            "func side() { print(\"called\")\nreturn true }\n"
            "print(false and side())\n"
            "print(true or side())\n"
            "print(true and side())\n"
            "print(false or side())\n")
        vm_out, interp_out = run_both(source)
        self.assertEqual(vm_out, "false\ntrue\ncalled\ntrue\ncalled\ntrue\n")
        self.assertEqual(vm_out, interp_out)

    def test_and_or_return_values(self):
        vm_out, interp_out = run_both(
            "print(0 and 5)\nprint(3 and 5)\nprint(0 or 5)\n"
            "print(\"\" or \"fallback\")\nprint(1 or 2)")
        self.assertEqual(vm_out, "0\n5\n5\nfallback\n1\n")
        self.assertEqual(vm_out, interp_out)

    def test_annotated_code_runs_identically(self):
        vm_out, interp_out = run_both(
            "let x: number = 5\n"
            "func add(a: number, b: number) -> number { return a + b }\n"
            "print(add(x, 7))\n"
            "let s: string = \"hi {x}\"\nprint(s)")
        self.assertEqual(vm_out, "12\nhi 5\n")
        self.assertEqual(vm_out, interp_out)

    def test_input_eof_error(self):
        # Patch input() to raise EOFError so neither path blocks on
        # real stdin; both must report the same KabirError.
        def _eof(prompt=""):
            raise EOFError

        orig = _builtins.input
        _builtins.input = _eof
        try:
            err = run_vm_error("let x = input()\nprint(x)")
            interp = Interpreter("", filename="<test>")
            try:
                with redirect_stdout(io.StringIO()):
                    interp.run_source("let x = input()\nprint(x)",
                                      filename="<test>")
                interp_err = None
            except KabirError as exc:
                interp_err = exc
        finally:
            _builtins.input = orig
        self.assertIsNotNone(err)
        self.assertIn("No more input", str(err))
        self.assertIsNotNone(interp_err)
        self.assertIn("No more input", str(interp_err))

    def test_seeded_rng_matches_interpreter(self):
        source = ("seed(42)\nprint(randint(1, 100))\nprint(randint(1, 100))\n"
                  "print(choice([1, 2, 3, 4]))")
        vm_out, interp_out = run_both(source)
        self.assertEqual(vm_out, interp_out)


class TestMatchVm(unittest.TestCase):
    def test_all_pattern_kinds(self):
        vm_out, interp_out = run_both(
            "print(match 0 { 0 => \"zero\", _ => \"?\" })\n"
            "print(match \"a\" { \"a\" => 1, _ => 2 })\n"
            "print(match true { false => 1, true => 2 })\n"
            "print(match null { null => \"nil\", _ => \"?\" })\n"
            "print(match 5 { n => n + 1 })\n"
            "print(match 9 { n => \"bound\", _ => \"?\" })")
        self.assertEqual(vm_out, "zero\n1\n2\nnil\n6\nbound\n")
        self.assertEqual(vm_out, interp_out)

    def test_guards(self):
        vm_out, interp_out = run_both(
            "print(match 3 { n if n > 10 => \"big\", n if n > 1 => \"mid\", "
            "_ => \"small\" })\n"
            "print(match 0 { n if n > 1 => \"big\", _ => \"small\" })")
        self.assertEqual(vm_out, "mid\nsmall\n")
        self.assertEqual(vm_out, interp_out)

    def test_guard_binding_visible_in_body(self):
        vm_out, interp_out = run_both(
            "print(match 7 { n if n > 5 => n * 2, _ => 0 })")
        self.assertEqual(vm_out, "14\n")
        self.assertEqual(vm_out, interp_out)

    def test_no_match_error(self):
        err = run_vm_error('match 5 { 0 => "zero", 1 => "one" }')
        self.assertIsNotNone(err)
        self.assertIn("No pattern matched the value 5", str(err))

    def test_binding_does_not_leak(self):
        err = run_vm_error("let r = match 7 { n => n + 1 }\nprint(n)")
        self.assertIsNotNone(err)
        self.assertIn("Undefined variable 'n'", str(err))

    def test_match_inside_function_and_nested(self):
        vm_out, interp_out = run_both(
            "func describe(n) { return match n { 0 => \"zero\", "
            "n if n < 0 => \"negative\", _ => \"positive\" } }\n"
            "print(describe(0))\nprint(describe(-4))\nprint(describe(7))\n"
            "print(match match 3 { 3 => 10, _ => 20 } "
            "{ 10 => \"ten\", _ => \"other\" })")
        self.assertEqual(vm_out, "zero\nnegative\npositive\nten\n")
        self.assertEqual(vm_out, interp_out)

    def test_match_as_statement(self):
        vm_out, interp_out = run_both(
            "match 0 { 0 => print(\"zero\"), _ => print(\"other\") }")
        self.assertEqual(vm_out, "zero\n")
        self.assertEqual(vm_out, interp_out)


class TestImportsVm(unittest.TestCase):
    def _write(self, directory, name, content):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_import_function_and_let(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "lib.kbr",
                        "func greet(name) { return \"Hello, \" + name }\n"
                        "let answer = 42\n")
            main = self._write(tmp, "main.kbr",
                               "import \"lib.kbr\"\n"
                               "print(greet(\"Rafid\"))\nprint(answer)\n")
            self.assertEqual(run_vm_file(main), "Hello, Rafid\n42\n")
            self.assertEqual(run_vm_file(main), run_interp_file(main))

    def test_import_isolation_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "lib.kbr",
                        "let x = 1\nfunc get_x() { return x }\n")
            main = self._write(tmp, "main.kbr",
                               "import \"lib.kbr\"\nlet x = 99\n"
                               "print(get_x())\nprint(x)\n"
                               "import \"lib.kbr\"\nprint(get_x())\n")
            self.assertEqual(run_vm_file(main), "1\n99\n1\n")
            self.assertEqual(run_vm_file(main), run_interp_file(main))

    def test_nested_and_diamond_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "d.kbr",
                        "print(\"d loaded\")\nfunc dfunc() { return 1 }\n")
            self._write(tmp, "b.kbr", "import \"d.kbr\"\nlet b_fn = dfunc\n")
            self._write(tmp, "c.kbr", "import \"d.kbr\"\nlet c_fn = dfunc\n")
            main = self._write(tmp, "main.kbr",
                               "import \"b.kbr\"\nimport \"c.kbr\"\n"
                               "print(b_fn == c_fn)\n")
            self.assertEqual(run_vm_file(main), "d loaded\ntrue\n")
            self.assertEqual(run_vm_file(main), run_interp_file(main))

    def test_import_cycle_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "a.kbr", "import \"b.kbr\"\n")
            self._write(tmp, "b.kbr", "import \"a.kbr\"\n")
            main = os.path.join(tmp, "a.kbr")
            with open(main, encoding="utf-8") as handle:
                source = handle.read()
            err = run_vm_error(source, filename=main)
            self.assertIsNotNone(err)
            self.assertIn("Import cycle detected", str(err))

    def test_import_missing_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = self._write(tmp, "main.kbr", "import \"missing.kbr\"\n")
            with open(main, encoding="utf-8") as handle:
                source = handle.read()
            err = run_vm_error(source, filename=main)
            self.assertIsNotNone(err)
            self.assertIn("no such file", str(err))
            self.assertIn("missing.kbr", str(err))

    def test_import_inside_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "lib.kbr", "let answer = 42\n")
            main = self._write(
                tmp, "main.kbr",
                "func load_it() { import \"lib.kbr\"\nreturn answer }\n"
                "print(load_it())\n")
            self.assertEqual(run_vm_file(main), "42\n")
            self.assertEqual(run_vm_file(main), run_interp_file(main))

    def test_failed_import_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = self._write(tmp, "lib.kbr", "print(undefined_var)\n")
            main = self._write(tmp, "main.kbr",
                               "import \"lib.kbr\"\nprint(answer)\n")
            with open(main, encoding="utf-8") as handle:
                source = handle.read()
            vm = VM(source, filename=main)
            with self.assertRaises(KabirError):
                vm.run_source(source, filename=main)
            self.assertEqual(vm._importing, [])
            canonical = os.path.realpath(os.path.abspath(lib))
            self.assertNotIn(canonical, vm.modules)
            self._write(tmp, "lib.kbr", "let answer = 42\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_source(source, filename=main)
            self.assertEqual(buf.getvalue(), "42\n")


class TestBytecodeRoundTrip(unittest.TestCase):
    def test_compile_save_load_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hello.kbc")
            code = compile_source("let name = \"Rafid\"\n"
                                  "print(\"Hello, {name}!\")\n")
            save_bytecode(code, path)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["magic"], "KBC1")
            self.assertEqual(data["version"], 2)
            self.assertEqual(data["main"], code)
            loaded = load_bytecode(path)
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(loaded)
            self.assertEqual(buf.getvalue(), "Hello, Rafid!\n")

    def test_round_trip_matches_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prog.kbc")
            source = ("func fib(n) { if n < 2 { return n } "
                      "return fib(n - 1) + fib(n - 2) }\n"
                      "for i in range(10) { print(fib(i)) }\n")
            save_bytecode(compile_source(source), path)
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(load_bytecode(path))
            self.assertEqual(buf.getvalue(), run_interp(source))

    def test_bad_magic_and_version_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.kbc")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump({"magic": "NOPE", "version": 1, "main": {}}, handle)
            with self.assertRaises(KabirError) as ctx:
                load_bytecode(bad)
            self.assertIn("bad magic", str(ctx.exception))
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump({"magic": "KBC1", "version": 99, "main": {}}, handle)
            with self.assertRaises(KabirError) as ctx:
                load_bytecode(bad)
            self.assertIn("unsupported version", str(ctx.exception))
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(KabirError):
                load_bytecode(bad)

    def test_compile_source_and_program_apis(self):
        code = compile_source("print(1 + 2)\n", filename="<t>")
        self.assertEqual(code["filename"], "<t>")
        self.assertEqual(code["name"], "<main>")
        self.assertEqual(code["params"], [])
        self.assertEqual(len(code["instrs"]), len(code["lines"]))
        for instr in code["instrs"]:
            self.assertIsInstance(instr, list)
            self.assertEqual(len(instr), 2)
        program = parse("print(3)\n", "<t>")
        code2 = compile_program(program, "<t>", ["print(3)"])
        vm = VM()
        buf = io.StringIO()
        with redirect_stdout(buf):
            vm.run_code(code2)
        self.assertEqual(buf.getvalue(), "3\n")


class TestCompileErrors(unittest.TestCase):
    def test_break_outside_loop(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("break\n")
        self.assertIn("'break' can only be used inside a loop",
                      str(ctx.exception))

    def test_continue_outside_loop(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("continue\n")
        self.assertIn("'continue' can only be used inside a loop",
                      str(ctx.exception))

    def test_return_outside_function(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("return 1\n")
        self.assertIn("'return' can only be used inside a function",
                      str(ctx.exception))

    def test_break_inside_function_without_loop(self):
        with self.assertRaises(KabirError) as ctx:
            compile_source("func f() { break }\n")
        self.assertIn("'break' can only be used inside a loop",
                      str(ctx.exception))

    def test_return_inside_function_is_fine(self):
        code = compile_source("func f() { return 1 }\nprint(f())\n")
        vm = VM()
        buf = io.StringIO()
        with redirect_stdout(buf):
            vm.run_code(code)
        self.assertEqual(buf.getvalue(), "1\n")


class TestExampleEquivalence(unittest.TestCase):
    """Every example file: VM stdout == interpreter stdout."""

    def _compare(self, name, inputs=None, seed=None):
        path = os.path.join(EXAMPLES, name)
        vm_out = run_vm_file(path, inputs, seed=seed)
        interp_out = run_interp_file(path, inputs, seed=seed)
        self.assertEqual(vm_out, interp_out, name)
        return vm_out

    def test_hello(self):
        self.assertEqual(self._compare("hello.kbr"),
                         "Hello, Rafid!\n"
                         "Welcome to Kabir version 4.1.0.\n2 + 3 = 5\n")

    def test_fizzbuzz(self):
        out = self._compare("fizzbuzz.kbr")
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[14], "FizzBuzz")

    def test_fibonacci(self):
        out = self._compare("fibonacci.kbr")
        self.assertIn("377", out)

    def test_primes(self):
        out = self._compare("primes.kbr")
        self.assertIn("97", out)

    def test_sorting(self):
        out = self._compare("sorting.kbr")
        self.assertIn("After:  [11, 12, 22, 25, 34, 64, 90]", out)

    def test_import_demo(self):
        self.assertEqual(
            self._compare("import-demo.kbr"),
            "Hello, Rafid!\nimported from greet-lib.kbr\n")

    def test_calculator(self):
        self.assertEqual(
            self._compare("calculator.kbr", ["2 + 3", "10 / 4", "7 * 6",
                                             "quit"]),
            run_interp_file(os.path.join(EXAMPLES, "calculator.kbr"),
                            ["2 + 3", "10 / 4", "7 * 6", "quit"]))

    def test_todo(self):
        out = self._compare("todo.kbr", ["add Buy milk", "add Learn Kabir",
                                         "list", "done 1", "list", "quit"])
        self.assertIn("Goodbye! You have 2 todos", out)

    def test_guessing_game_seeded(self):
        # Seed both RNGs identically so the secret matches.
        guesses = [str(n) for n in range(1, 101)]
        out = self._compare("guessing-game.kbr", guesses, seed=7)
        self.assertIn("You got it in", out)


class TestCliRunPaths(unittest.TestCase):
    def _run_cli(self, argv, cwd=None):
        from kabir.cli import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_run_vm_default_and_interp_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prog.kbr")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("print(6 * 7)\n")
            code, out, err = self._run_cli(["run", path])
            self.assertEqual((code, out), (0, "42\n"))
            code, out, err = self._run_cli(["run", "--interp", path])
            self.assertEqual((code, out), (0, "42\n"))

    def test_run_kbc_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            kbc = os.path.join(tmp, "prog.kbc")
            save_bytecode(compile_source("print(\"from bytecode\")\n"), kbc)
            code, out, err = self._run_cli(["run", kbc])
            self.assertEqual((code, out), (0, "from bytecode\n"))
            code, out, err = self._run_cli([kbc])
            self.assertEqual((code, out), (0, "from bytecode\n"))

    def test_run_vm_error_exit_code_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.kbr")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("print(undefined_var)\n")
            code, out, err = self._run_cli(["run", path])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            self.assertIn("Undefined variable 'undefined_var'", err)

    def test_run_kbc_missing_file(self):
        code, out, err = self._run_cli(["run", "/nonexistent/x.kbc"])
        self.assertEqual(code, 2)
        self.assertIn("cannot open", err)

    def test_run_too_many_args(self):
        code, out, err = self._run_cli(["run", "a.kbr", "b.kbr"])
        self.assertEqual(code, 2)

    def test_help_mentions_new_options(self):
        code, out, err = self._run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("kabir run --interp", out)
        self.assertIn("kabir run <file.kbc>", out)


if __name__ == "__main__":
    unittest.main()
