"""Slot-based compiler tests (Kabir 3.0.0 Wave 0).

Covers: LOAD_SLOT / STORE_SLOT / DEFINE_SLOT emission, parameters in
slots, captured variables staying name-based (closures keep working),
recursion, shadowing in nested blocks, VM error columns, .kbc v1
still loading, and v2 round-trip.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.compile import (MAGIC, VERSION, compile_source, load_bytecode,
                           save_bytecode)
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.vm import VM


def run_vm(source, filename="<test>"):
    vm = VM(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm.run_source(source, filename=filename)
    return buf.getvalue()


def run_interp(source, filename="<test>"):
    interp = Interpreter(source, filename=filename)
    buf = io.StringIO()
    with redirect_stdout(buf):
        interp.run_source(source, filename=filename)
    return buf.getvalue()


def run_vm_error(source, filename="<test>"):
    vm = VM(source, filename=filename)
    try:
        vm.run_source(source, filename=filename)
    except KabirError as err:
        return err
    return None


def run_interp_error(source, filename="<test>"):
    interp = Interpreter(source, filename=filename)
    try:
        interp.run_source(source, filename=filename)
    except KabirError as err:
        return err
    return None


def code_of(source):
    return compile_source(source, filename="<test>")


def func_code(code, name):
    """The nested code dict for the function named *name*."""
    for const in code["consts"]:
        if isinstance(const, dict) and "__code__" in const:
            inner = const["__code__"]
            if inner["name"] == name:
                return inner
    return None


def ops(code):
    return [op for op, _arg in code["instrs"]]


class TestSlotEmission(unittest.TestCase):
    def test_version_is_2_and_shape(self):
        self.assertEqual(VERSION, 2)
        code = code_of("func f() { let x = 1\nreturn x }\nprint(f())\n")
        self.assertEqual(len(code["instrs"]), len(code["lines"]))
        self.assertEqual(len(code["instrs"]), len(code["cols"]))
        fn = func_code(code, "f")
        self.assertEqual(fn["locals"], ["x"])
        self.assertEqual(len(fn["instrs"]), len(fn["cols"]))
        for col in code["cols"] + fn["cols"]:
            self.assertIsInstance(col, int)
            self.assertGreaterEqual(col, 1)

    def test_top_level_lets_stay_name_based(self):
        # Globals are never slot-allocated: imported modules export
        # exactly the bindings in their env.
        code = code_of("let x = 1\nfunc get_x() { return x }\n"
                       "print(get_x())\n")
        self.assertEqual(code["locals"], [])
        self.assertNotIn("DEFINE_SLOT", ops(code))
        self.assertNotIn("LOAD_SLOT", ops(code))
        self.assertIn("DEFINE_VAR", ops(code))

    def test_let_uses_define_and_load_slot(self):
        code = code_of("func f() { let x = 1\nreturn x + 2 }\n"
                       "print(f())\n")
        fn = func_code(code, "f")
        self.assertIn("DEFINE_SLOT", ops(fn))
        self.assertIn("LOAD_SLOT", ops(fn))
        self.assertNotIn("DEFINE_VAR", ops(fn))
        self.assertEqual(fn["locals"], ["x"])
        self.assertEqual(run_vm("func f() { let x = 1\nreturn x + 2 }\n"
                                "print(f())\n"), "3\n")

    def test_params_occupy_slots_in_order(self):
        code = code_of("func add(a, b) { let c = a + b\nreturn c }\n"
                       "print(add(2, 3))\n")
        fn = func_code(code, "add")
        self.assertIsNotNone(fn)
        self.assertEqual(fn["params"], ["a", "b"])
        self.assertEqual(fn["locals"], ["a", "b", "c"])
        self.assertIn("LOAD_SLOT", ops(fn))
        self.assertIn("DEFINE_SLOT", ops(fn))
        self.assertEqual(run_vm("func add(a, b) { let c = a + b\n"
                                "return c }\nprint(add(2, 3))\n"), "5\n")

    def test_store_slot_for_assignment_and_compound(self):
        source = ("func f() { let x = 1\nx = 2\nx += 3\nreturn x }\n"
                  "print(f())\n")
        fn = func_code(code_of(source), "f")
        self.assertIn("STORE_SLOT", ops(fn))
        self.assertEqual(run_vm(source), "5\n")

    def test_captured_let_stays_name_based(self):
        code = code_of("let x = 1\nfunc get_x() { return x }\n"
                       "print(get_x())\n")
        # x is referenced by the nested function, so the top level
        # keeps it in the env: no slot for it anywhere in <main>.
        self.assertEqual(code["locals"], [])
        self.assertIn("DEFINE_VAR", ops(code))
        self.assertNotIn("DEFINE_SLOT", ops(code))
        self.assertEqual(run_vm("let x = 1\nfunc get_x() { return x }\n"
                                "print(get_x())\n"), "1\n")

    def test_captured_param_stays_name_based(self):
        code = code_of("func outer(a) { func inner() { return a }\n"
                       "return inner() }\nprint(outer(21))\n")
        outer = func_code(code, "outer")
        self.assertIsNotNone(outer)
        # Slot 0 is a captured parameter: locals[0] is None, so the
        # VM passes it via the env instead of a slot. (The function
        # name inner itself is not captured, so it takes slot 1.)
        self.assertEqual(outer["locals"], [None, "inner"])
        inner = func_code(outer, "inner")
        self.assertIsNotNone(inner)
        self.assertIn("LOAD_VAR", ops(inner))
        self.assertNotIn("LOAD_SLOT", ops(inner))
        self.assertEqual(run_vm("func outer(a) { func inner() { return a }\n"
                                "return inner() }\nprint(outer(21))\n"),
                         "21\n")

    def test_uncaptured_param_uses_slot(self):
        code = code_of("func f(a) { return a + 1 }\nprint(f(41))\n")
        fn = func_code(code, "f")
        self.assertEqual(fn["locals"], ["a"])
        self.assertIn("LOAD_SLOT", ops(fn))
        self.assertEqual(run_vm("func f(a) { return a + 1 }\n"
                                "print(f(41))\n"), "42\n")

    def test_relet_gets_a_fresh_slot(self):
        code = code_of("func f() { let x = 1\nlet x = 2\nreturn x }\n"
                       "print(f())\n")
        fn = func_code(code, "f")
        self.assertEqual(fn["locals"], ["x", "x"])
        self.assertEqual(run_vm("func f() { let x = 1\nlet x = 2\n"
                                "return x }\nprint(f())\n"), "2\n")

    def test_for_variable_in_slot(self):
        source = ("func f() { let s = 0\nfor i in range(3) { s += i }\n"
                  "return s }\nprint(f())\n")
        fn = func_code(code_of(source), "f")
        self.assertIn("i", fn["locals"])
        self.assertIn("s", fn["locals"])
        self.assertEqual(run_vm(source), "3\n")

    def test_match_binding_in_slot(self):
        code = code_of("func f() { return match 7 { n => n * 2 } }\n"
                       "print(f())\n")
        fn = func_code(code, "f")
        self.assertIn("DEFINE_SLOT", ops(fn))
        self.assertEqual(run_vm("func f() { return match 7 { n => n * 2 } }\n"
                                "print(f())\n"), "14\n")

    def test_match_binding_captured_by_nested_function(self):
        source = ("func f() { return match 7 { n => func() { return n } } }\n"
                  "let g = f()\nprint(g())\n")
        fn = func_code(code_of(source), "f")
        self.assertIn("DEFINE_VAR", ops(fn))
        self.assertNotIn("DEFINE_SLOT", ops(fn))
        self.assertEqual(run_vm(source), "7\n")


class TestSlotsRuntime(unittest.TestCase):
    def test_closure_still_works(self):
        source = ("func make_adder(n) { return func(x) { return x + n } }\n"
                  "let add5 = make_adder(5)\nprint(add5(10))\nprint(add5(1))\n")
        self.assertEqual(run_vm(source), "15\n6\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_closure_captures_through_interpolation(self):
        source = ("let x = 5\nfunc f() { return \"{x}\" }\nprint(f())\n")
        self.assertEqual(run_vm(source), "5\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_closure_captures_let_defined_after_it(self):
        # The nested function references x before x is defined in
        # the outer scope; x must still stay name-based so the
        # closure sees the later definition.
        source = ("func get_x() { return x }\nlet x = 42\nprint(get_x())\n")
        self.assertEqual(run_vm(source), "42\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_closure_mutates_captured_variable(self):
        source = ("let total = 0\nfunc add(n) { total += n }\n"
                  "for i in range(4) { add(i) }\nprint(total)\n")
        self.assertEqual(run_vm(source), "6\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_recursion(self):
        source = ("func fib(n) { if n < 2 { return n } "
                  "return fib(n - 1) + fib(n - 2) }\nprint(fib(15))\n")
        self.assertEqual(run_vm(source), "610\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_recursive_anonymous_function_via_let(self):
        source = ("let fact = func(n) { if n < 2 { return 1 } "
                  "return n * fact(n - 1) }\nprint(fact(5))\n")
        self.assertEqual(run_vm(source), "120\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_shadowing_in_nested_blocks(self):
        source = ("let x = 1\nif true { let x = 2\nprint(x) }\nprint(x)\n")
        self.assertEqual(run_vm(source), "2\n1\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_shadowing_param_in_block(self):
        source = ("func f(x) { if true { let x = 2\nprint(x) }\n"
                  "return x }\nprint(f(1))\n")
        self.assertEqual(run_vm(source), "2\n1\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_let_does_not_leak_from_block(self):
        err = run_vm_error("if true { let hidden = 1 }\nprint(hidden)")
        self.assertIsNotNone(err)
        self.assertIn("Undefined variable 'hidden'", str(err))

    def test_relet_visible_to_earlier_closure(self):
        source = ("let x = 1\nfunc get_x() { return x }\nlet x = 99\n"
                  "print(get_x())\nprint(x)\n")
        self.assertEqual(run_vm(source), "99\n99\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_match_with_captured_outer_variable(self):
        source = "let k = 3\nprint(match 7 { n => n + k })\n"
        self.assertEqual(run_vm(source), "10\n")
        self.assertEqual(run_vm(source), run_interp(source))

    def test_imported_module_exports_survive_slots(self):
        # Regression: module-level names must stay in the env so the
        # importer receives them, even though the module's own
        # functions use slots internally.
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, "lib.kbr")
            with open(lib, "w", encoding="utf-8") as handle:
                handle.write("func greet(name) { return \"Hello, \" + name }\n"
                             "let answer = 42\n"
                             "func get_answer() { return answer }\n")
            main = os.path.join(tmp, "main.kbr")
            with open(main, "w", encoding="utf-8") as handle:
                handle.write("import \"lib.kbr\"\n"
                             "print(greet(\"Rafid\"))\nprint(answer)\n"
                             "print(get_answer())\n")
            with open(main, encoding="utf-8") as handle:
                source = handle.read()
            vm = VM(source, filename=main)
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_source(source, filename=main)
            self.assertEqual(buf.getvalue(),
                             "Hello, Rafid\n42\n42\n")

    def test_slots_and_env_agree_on_examples(self):
        # A slot-heavy program: locals, loops, shadowing, closures.
        source = ("func sum_to(n) { let total = 0\n"
                  "for i in range(n + 1) { total += i }\nreturn total }\n"
                  "print(sum_to(10))\n"
                  "func make_counter() { let c = 0\n"
                  "return func() { c += 1\nreturn c } }\n"
                  "let next = make_counter()\n"
                  "print(next())\nprint(next())\n")
        self.assertEqual(run_vm(source), "55\n1\n2\n")
        self.assertEqual(run_vm(source), run_interp(source))


class TestErrorColumns(unittest.TestCase):
    def test_vm_error_shows_line_and_col(self):
        err = run_vm_error("print(1 / 0)\n")
        self.assertIsNotNone(err)
        self.assertEqual(err.line, 1)
        self.assertIsNotNone(err.col)
        self.assertIn(":1:%d:" % err.col, str(err))
        self.assertIn("Division by zero", str(err))

    def test_vm_error_col_matches_interpreter(self):
        cases = [
            "print(1 / 0)\n",
            "let x = [1]\nprint(x[5])\n",
            "print(undefined_var)\n",
            "let a = 1\nprint(a + \"s\")\n",
            "print(len(1))\n",
            "func f(n) { return n / 0 }\nprint(f(1))\n",
            "for x in 5 { print(x) }\n",
            "print(-\"a\")\n",
            "let a = [1]\na[5] = 9\n",
            "let d = {\"a\": 1}\nprint(d[\"b\"])\n",
        ]
        for source in cases:
            with self.subTest(source=source):
                vm_err = run_vm_error(source)
                interp_err = run_interp_error(source)
                self.assertIsNotNone(vm_err, source)
                self.assertIsNotNone(interp_err, source)
                self.assertEqual(vm_err.line, interp_err.line, source)
                self.assertEqual(vm_err.col, interp_err.col, source)

    def test_error_inside_function_reports_its_position(self):
        source = ("func f() {\n  let x = 1\n  return x / 0\n}\n"
                  "print(f())\n")
        err = run_vm_error(source)
        self.assertIsNotNone(err)
        self.assertEqual(err.line, 3)
        self.assertIsNotNone(err.col)
        self.assertIn(":3:%d:" % err.col, str(err))


class TestBytecodeVersions(unittest.TestCase):
    def test_v2_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prog.kbc")
            source = ("func fib(n) { if n < 2 { return n } "
                      "return fib(n - 1) + fib(n - 2) }\n"
                      "for i in range(10) { print(fib(i)) }\n")
            code = compile_source(source)
            save_bytecode(code, path)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["magic"], "KBC1")
            self.assertEqual(data["version"], 2)
            self.assertEqual(data["main"], code)
            self.assertIn("cols", data["main"])
            self.assertIn("locals", data["main"])
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(load_bytecode(path))
            self.assertEqual(buf.getvalue(), run_interp(source))

    def test_v1_bytecode_still_loads_and_runs(self):
        # A hand-built v1 code dict: name-based ops only, no "cols"
        # or "locals" keys anywhere.
        v1_main = {
            "name": "<main>", "params": [],
            "instrs": [["LOAD_VAR", "print"], ["LOAD_CONST", 0],
                       ["CALL", 1], ["POP", None],
                       ["LOAD_NULL", None], ["RETURN", None]],
            "consts": [42],
            "lines": [1, 1, 1, 1, 1, 1],
            "filename": "<test>",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.kbc")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"magic": MAGIC, "version": 1, "main": v1_main},
                          handle)
            loaded = load_bytecode(path)
            self.assertEqual(loaded, v1_main)
            self.assertNotIn("cols", loaded)
            self.assertNotIn("locals", loaded)
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(loaded)
            self.assertEqual(buf.getvalue(), "42\n")

    def test_v1_function_code_still_runs(self):
        # v1 nested function: params passed via env, no slots.
        v1_fn = {
            "name": "double", "params": ["n"],
            "instrs": [["LOAD_VAR", "n"], ["LOAD_CONST", 0],
                       ["MUL", None], ["RETURN", None]],
            "consts": [2],
            "lines": [1, 1, 1, 1],
            "filename": "<test>",
        }
        v1_main = {
            "name": "<main>", "params": [],
            "instrs": [["LOAD_CONST", 0], ["MAKE_FUNCTION", 0],
                       ["DEFINE_VAR", "double"],
                       ["LOAD_VAR", "print"], ["LOAD_VAR", "double"],
                       ["LOAD_CONST", 1], ["CALL", 1], ["CALL", 1],
                       ["POP", None], ["LOAD_NULL", None],
                       ["RETURN", None]],
            "consts": [{"__code__": v1_fn}, 21],
            "lines": [1] * 11,
            "filename": "<test>",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "oldfn.kbc")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"magic": MAGIC, "version": 1, "main": v1_main},
                          handle)
            vm = VM()
            buf = io.StringIO()
            with redirect_stdout(buf):
                vm.run_code(load_bytecode(path))
            self.assertEqual(buf.getvalue(), "42\n")

    def test_unsupported_version_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.kbc")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"magic": MAGIC, "version": 99, "main": {}},
                          handle)
            with self.assertRaises(KabirError) as ctx:
                load_bytecode(path)
            self.assertIn("unsupported version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
