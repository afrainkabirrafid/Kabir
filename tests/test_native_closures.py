"""Tests for native closures (7.0.0 Leap 2): Kabir AST -> C99 -> binary.

Captured variables live in arena slots shared with the defining
function, so a closure keeps working after its defining function
returns, `let mut` captures stay mutable, and every loop iteration
gets fresh captures. Each case runs the same program on the VM and
on native and compares stdout.
"""

import contextlib
import io
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kabir import codegen
from kabir.checker import check_program
from kabir.compile import compile_program
from kabir.errors import KabirError
from kabir.parser import parse as parse_source
from kabir.vm import VM

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
NEEDS_CC = unittest.skipIf(CC is None, "no C compiler (cc/gcc/clang)")


def run_vm(source, filename="<test>"):
    """Run *source* on the VM; return (exit_code, stdout, stderr)."""
    lines = source.split("\n")
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            program = parse_source(source, filename)
            check_program(program, filename=filename, lines=lines)
            code = compile_program(program, filename, lines)
            VM(source, filename=filename).run_code(
                code, filename=filename, lines=lines)
    except KabirError as err:
        return 1, buf.getvalue(), err.render()
    except RecursionError:
        return 1, buf.getvalue(), "too many nested function calls"
    finally:
        sys.stdin = old_stdin
    return 0, buf.getvalue(), ""


@NEEDS_CC
class TestNativeClosures(unittest.TestCase):
    """assert_same_output runs a program on the VM and on native."""

    def assert_same_output(self, source):
        vm_code, vm_out, vm_err = run_vm(source)
        self.assertEqual(vm_code, 0, "VM failed: %s" % vm_err)
        nat_code, nat_out, nat_err = codegen.run_native(
            source, use_cache=False)
        self.assertEqual(nat_code, 0, "native failed: %s" % nat_err)
        self.assertEqual(nat_out, vm_out,
                         "native stdout differs from VM stdout")

    def test_simple_capture(self):
        self.assert_same_output(
            "func make_adder(x) { return func(y) { return x + y } }\n"
            "let add5 = make_adder(5)\n"
            "print(add5(3))\n")

    def test_capture_mutation(self):
        # `let mut` capture: the closure mutates the shared cell and
        # the defining function's other closures see it.
        self.assert_same_output(
            "func make_counter() {\n"
            "    let mut n = 0\n"
            "    let inc = func() { n += 1 return n }\n"
            "    return inc\n"
            "}\n"
            "let c = make_counter()\n"
            "print(c())\n"
            "print(c())\n"
            "print(c())\n")

    def test_shared_mutation_between_closures(self):
        self.assert_same_output(
            "func make_pair() {\n"
            "    let mut n = 0\n"
            "    let inc = func() { n += 1 return n }\n"
            "    let get = func() { return n }\n"
            "    inc()\n"
            "    inc()\n"
            "    return get()\n"
            "}\n"
            "print(make_pair())\n")

    def test_mutation_visible_to_definer(self):
        self.assert_same_output(
            "func make() {\n"
            "    let mut n = 10\n"
            "    let get = func() { return n }\n"
            "    let set = func(v) { n = v }\n"
            "    set(42)\n"
            "    return get()\n"
            "}\n"
            "print(make())\n")

    def test_capture_mutated_parameter(self):
        self.assert_same_output(
            "func count_from(start) {\n"
            "    let mut n = start\n"
            "    return func() { n += 1 return n }\n"
            "}\n"
            "let c = count_from(100)\n"
            "print(c())\n"
            "print(c())\n")

    def test_returned_closure(self):
        self.assert_same_output(
            "func make_adder(x) { return func(y) { return x + y } }\n"
            "let add5 = make_adder(5)\n"
            "let add100 = make_adder(100)\n"
            "print(add5(3) + add100(3))\n")

    def test_closure_passed_as_argument(self):
        self.assert_same_output(
            "func apply(f, x) { return f(x) }\n"
            "func make_mul(n) { return func(x) { return x * n } }\n"
            "print(apply(make_mul(3), 7))\n")

    def test_closure_stored_in_array(self):
        self.assert_same_output(
            "func make_adder(x) { return func(y) { return x + y } }\n"
            "let fs = [make_adder(1), make_adder(10)]\n"
            "print(fs[0](100) + fs[1](100))\n")

    def test_nested_closures_three_levels(self):
        self.assert_same_output(
            "func outer(a) {\n"
            "    func mid(b) {\n"
            "        func inner(c) { return a + b + c }\n"
            "        return inner\n"
            "    }\n"
            "    return mid\n"
            "}\n"
            "let f = outer(1)(2)\n"
            "print(f(3))\n"
            "print(outer(10)(20)(30))\n")

    def test_loop_collected_closures_fresh_each_iteration(self):
        # Each iteration's `let x` is a fresh capture: fs[0]() is 0,
        # not the last iteration's 20.
        self.assert_same_output(
            "func f() {\n"
            "    let fs = []\n"
            "    for i in range(3) {\n"
            "        let x = i * 10\n"
            "        push(fs, func() { return x })\n"
            "    }\n"
            "    print(fs[0]() + fs[1]() + fs[2]())\n"
            "}\n"
            "f()\n")

    def test_loop_variable_capture_fresh_each_iteration(self):
        self.assert_same_output(
            "let fs = []\n"
            "for i in range(3) {\n"
            "    push(fs, func() { return i })\n"
            "}\n"
            "print(fs[0]() + fs[1]() + fs[2]())\n")

    def test_forward_reference_capture(self):
        # The closure is created before its captured `let` runs;
        # it reads the value once called afterwards.
        self.assert_same_output(
            "func outer() {\n"
            "    func get() { return x }\n"
            "    let x = 5\n"
            "    return get()\n"
            "}\n"
            "print(outer())\n")

    def test_same_scope_redefinition_seen_by_closure(self):
        self.assert_same_output(
            "func f() {\n"
            "    let x = 1\n"
            "    let g = func() { return x }\n"
            "    let x = 2\n"
            "    return g()\n"
            "}\n"
            "print(f())\n")

    def test_top_level_block_capture(self):
        self.assert_same_output(
            "if true {\n"
            "    let x = 7\n"
            "    func f() { return x }\n"
            "    print(f())\n"
            "}\n")

    def test_match_binding_capture(self):
        self.assert_same_output(
            "func f(v) {\n"
            "    let g = match v {\n"
            "        n if n > 10 => func() { return n },\n"
            "        _ => func() { return 0 },\n"
            "    }\n"
            "    return g()\n"
            "}\n"
            "print(f(20))\n"
            "print(f(5))\n")

    def test_local_shadows_global(self):
        self.assert_same_output(
            "let x = 99\n"
            "func outer() {\n"
            "    let x = 1\n"
            "    let get = func() { return x }\n"
            "    return get()\n"
            "}\n"
            "print(outer())\n"
            "print(x)\n")

    def test_interpolation_capture(self):
        self.assert_same_output(
            "func greeter(name) {\n"
            "    return func() { return \"hi {name}\" }\n"
            "}\n"
            "print(greeter(\"kat\")())\n")

    def test_recursive_nested_function(self):
        self.assert_same_output(
            "func outer() {\n"
            "    func f(n) {\n"
            "        if n <= 0 { return 0 }\n"
            "        return f(n - 1) + 1\n"
            "    }\n"
            "    return f(4)\n"
            "}\n"
            "print(outer())\n")

    def test_closure_used_inside_loop(self):
        self.assert_same_output(
            "func make_counter() {\n"
            "    let mut n = 0\n"
            "    return func() { n += 1 return n }\n"
            "}\n"
            "let c = make_counter()\n"
            "let mut total = 0\n"
            "for i in range(5) { total += c() }\n"
            "print(total)\n")

    def test_index_mutation_through_capture(self):
        self.assert_same_output(
            "func f() {\n"
            "    let arr = [1, 2]\n"
            "    let bump = func() { arr[0] = arr[0] + 10 return arr[0] }\n"
            "    print(bump())\n"
            "    print(bump())\n"
            "    print(arr[0])\n"
            "}\n"
            "f()\n")


if __name__ == "__main__":
    unittest.main()
