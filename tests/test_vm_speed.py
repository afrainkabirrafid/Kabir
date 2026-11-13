"""Tests for the 5.0.0 Leap 3 VM optimizations.

These guard the risky parts: frames that share their closure env
instead of allocating a fresh Environment per call, blocks that skip
their ENTER_SCOPE/EXIT_SCOPE pair when they define no name, and
string methods as bound-method values instead of per-access
closures. Every case asserts VM output (and, where it matters, the
interpreter's output too) so a speed change that alters semantics
fails loudly.
"""

import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kabir.checker import check_program
from kabir.compile import compile_program
from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.parser import parse as parse_source
from kabir.vm import VM


def run_vm(source):
    """Run *source* on the VM; return (exit_code, stdout, stderr)."""
    lines = source.split("\n")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            program = parse_source(source, "<test>")
            check_program(program, filename="<test>", lines=lines)
            code = compile_program(program, "<test>", lines)
            VM(source, filename="<test>").run_code(
                code, filename="<test>", lines=lines)
    except KabirError as err:
        return 1, buf.getvalue(), err.render()
    except RecursionError:
        return 1, buf.getvalue(), "too many nested function calls"
    return 0, buf.getvalue(), ""


def run_interp(source):
    """Run *source* on the tree-walking interpreter."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            Interpreter(source, filename="<test>").run_source(
                source, filename="<test>")
    except KabirError as err:
        return 1, buf.getvalue(), err.render()
    except RecursionError:
        return 1, buf.getvalue(), "too many nested function calls"
    return 0, buf.getvalue(), ""


class TestVmSpeedSemantics(unittest.TestCase):
    def assert_vm(self, source, expected):
        code, out, err = run_vm(source)
        self.assertEqual(code, 0, "VM failed: %s" % err)
        self.assertEqual(out, expected)

    def assert_vm_and_interp(self, source, expected):
        self.assert_vm(source, expected)
        code, out, err = run_interp(source)
        self.assertEqual(code, 0, "interp failed: %s" % err)
        self.assertEqual(out, expected,
                         "VM and interpreter disagree")

    # -- shared-env frames: closures must still capture correctly --
    def test_closure_captures_outer_local(self):
        self.assert_vm_and_interp('''
func make_adder(x) {
    func add(y) {
        return x + y
    }
    return add
}
let add5 = make_adder(5)
print(add5(3))
print(add5(10))
''', "8\n15\n")

    def test_closure_mutates_captured_variable(self):
        self.assert_vm_and_interp('''
func make_counter() {
    let n = 0
    func inc() {
        n = n + 1
        return n
    }
    return inc
}
let c = make_counter()
print(c())
print(c())
print(c())
''', "1\n2\n3\n")

    def test_recursive_function_with_captured_param(self):
        self.assert_vm_and_interp('''
func fib(n) {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}
print(fib(15))
''', "610\n")

    def test_function_assigning_a_global(self):
        # Assigning a global from inside a function must still hit
        # the global -- a shared-env frame shares the globals env.
        self.assert_vm_and_interp('''
let total = 0
func add_to_total(n) {
    total = total + n
}
add_to_total(5)
add_to_total(7)
print(total)
''', "12\n")

    def test_nested_function_does_not_leak_into_globals(self):
        # A let inside a function must not appear in the globals.
        code, out, err = run_vm('''
func f() {
    let hidden = 42
    return hidden
}
print(f())
print(hidden)
''')
        self.assertEqual(code, 1)
        self.assertIn("Undefined variable 'hidden'", err)

    def test_recursion_depth_error_still_raised(self):
        code, _out, err = run_vm('''
func boom(n) {
    return boom(n + 1)
}
print(boom(0))
''')
        self.assertEqual(code, 1)
        self.assertIn("Too many nested function calls", err)

    # -- scopeless blocks: shadowing, break/continue, temps --
    def test_block_shadowing_still_works(self):
        self.assert_vm_and_interp('''
let x = 1
if true {
    let x = 2
    print(x)
}
print(x)
''', "2\n1\n")

    def test_break_continue_in_scopeless_blocks(self):
        self.assert_vm_and_interp('''
let i = 0
while i < 5 {
    i = i + 1
    if i == 2 {
        continue
    }
    if i == 4 {
        break
    }
    print(i)
}
''', "1\n3\n")

    def test_compound_index_assign_in_scopeless_block(self):
        self.assert_vm_and_interp('''
let a = [1, 2, 3]
if true {
    a[0] += 10
    a[1] -= 1
}
print(a[0])
print(a[1])
print(a[2])
''', "11\n1\n3\n")

    def test_for_loop_variable_does_not_leak(self):
        code, _out, err = run_vm('''
for n in range(3) {
    print(n)
}
print(n)
''')
        self.assertEqual(code, 1)
        self.assertIn("Undefined variable 'n'", err)

    def test_while_loop_locals_do_not_leak(self):
        code, _out, err = run_vm('''
let i = 0
while i < 2 {
    let temp = i * 10
    print(temp)
    i = i + 1
}
print(temp)
''')
        self.assertEqual(code, 1)
        self.assertIn("Undefined variable 'temp'", err)

    # -- inlined arithmetic: errors and edge cases --
    def test_division_by_zero_still_errors(self):
        code, _out, err = run_vm('''
let x = 1 / 0
print(x)
''')
        self.assertEqual(code, 1)
        self.assertIn("Division by zero", err)

    def test_modulo_by_zero_still_errors(self):
        code, _out, err = run_vm('print(5 % 0)\n')
        self.assertEqual(code, 1)
        self.assertIn("Division by zero", err)

    def test_int_division_exact_vs_fractional(self):
        self.assert_vm_and_interp('''
print(7 / 2)
print(6 / 2)
print(7 % 3)
print(2 * 3 + 1)
print(10 - 4)
print(1 < 2)
print("a" < "b")
print(1 == 1.0)
print(true == 1)
''', "3.5\n3\n1\n7\n6\ntrue\ntrue\ntrue\nfalse\n")

    def test_undefined_variable_still_errors(self):
        code, _out, err = run_vm('print(nope)\n')
        self.assertEqual(code, 1)
        self.assertIn("Undefined variable 'nope'", err)

    def test_assign_to_undefined_still_errors(self):
        code, _out, err = run_vm('nope = 1\n')
        self.assertEqual(code, 1)
        self.assertIn("Cannot assign to 'nope'", err)

    # -- bound string methods --
    def test_bound_method_call_and_identity(self):
        self.assert_vm_and_interp('''
let s = "hello"
let m = s.upper
print(m())
print(type(m))
print(m)
print(s.upper == s.upper)
print(m == m)
''', "HELLO\nfunction\n<builtin function upper>\nfalse\ntrue\n")

    def test_string_method_errors_unchanged(self):
        code, _out, err = run_vm('print("x".nope())\n')
        self.assertEqual(code, 1)
        self.assertIn("No method '.nope' on a string", err)
        code, _out, err = run_vm('let d = {"a": 1}\nprint(d.foo())\n')
        self.assertEqual(code, 1)
        self.assertIn('Cannot use .foo on a dict', err)

    def test_and_or_short_circuit_unchanged(self):
        self.assert_vm_and_interp('''
print(true or false)
print(false and true)
print(1 == 1 and 2 == 2)
print(0 or "fallback")
''', "true\nfalse\ntrue\nfallback\n")


if __name__ == "__main__":
    unittest.main()
