"""Type checker tests (Kabir 2.0.0, design-doc section 2).

Tests call check_program directly -- the checker is not yet wired
into `kabir run`.
"""

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.checker import check_program
from kabir.errors import KabirError
from kabir.parser import parse


def check(source):
    """Type-check *source*; return None on success."""
    program = parse(source, "<test>")
    return check_program(program, filename="<test>",
                         lines=source.split("\n"))


def check_err(source, fragment):
    """Assert *source* fails type-checking with *fragment* in the error."""
    with unittest.TestCase().assertRaises(KabirError) as ctx:
        check(source)
    assert fragment in str(ctx.exception), (
        "expected %r in error, got: %s" % (fragment, ctx.exception))
    return ctx.exception


class TestValidTypedPrograms(unittest.TestCase):
    def test_annotated_let_correct(self):
        check('let x: number = 5\nlet s: string = "hi"\n'
              'let b: bool = true\nlet a: array = [1, 2]\n'
              'let d: dict = {"k": 1}\n'
              'let f: function = func (x) { return x }\n'
              'let u: any = 5\n')

    def test_annotated_let_inferred_from_expression(self):
        check('let x: number = 2 + 3\nlet s: string = "a" + "b"\n'
              'let b: bool = 1 < 2\nlet n: number = len("hi")\n'
              'let s2: string = str(42)\n')

    def test_annotated_function_and_call(self):
        check('func add(a: number, b: number) -> number '
              '{ return a + b }\n'
              'let r: number = add(2, 3)\nprint(r)')

    def test_annotated_function_return_variants(self):
        check('func greet(name: string) -> string '
              '{ return "hi " + name }\n'
              'func nothing() { return null }\n'
              'func bare() { return }\n')

    def test_partial_annotations(self):
        check('func f(a: number, b) { return a }\nprint(f(1, "x"))')

    def test_any_param_accepts_anything(self):
        check('func f(a: any) -> any { return a }\n'
              'print(f(1))\nprint(f("x"))\nprint(f(true))')

    def test_arithmetic_and_comparison_rules_pass(self):
        check('let a = 1 - 2 * 3 / 4 % 5 ** 2\n'
              'let s = "a" + "b"\nlet arr = [1] + [2]\n'
              'let b = 1 < 2\nlet c = "a" <= "b"\nlet d = 1 == "x"\n')

    def test_if_while_for_conditions_pass(self):
        check('if 1 < 2 { print("yes") }\n'
              'while false { print("no") }\n'
              'for x in [1, 2] { print(x) }\n'
              'for c in "abc" { print(c) }\n'
              'for k in {"a": 1} { print(k) }\n'
              'for i in range(3) { print(i) }\n')

    def test_builtin_calls_pass(self):
        check('let n: number = len("hi")\n'
              'let n2: number = len([1])\n'
              'let n3: number = len({"a": 1})\n'
              'let s: string = str(1)\n'
              'let s2: string = input("? ")\n'
              'let s3: string = input()\n'
              'let m: number = num("42")\n'
              'let t: string = type(1)\n'
              'let r: array = range(3)\n'
              'let r2: array = range(1, 5)\n'
              'let a: array = push([1], 2)\n'
              'let v = pop([1])\n'
              'let q: number = sqrt(4)\n'
              'let p: number = pow(2, 3)\n'
              'let ab: number = abs(0 - 5)\n'
              'let ks: array = keys({"a": 1})\n'
              'let i: number = int("3")\n'
              'let f: number = float(3)\n'
              'print("x", 1, true)\n')

    def test_stdlib_calls_pass(self):
        check('let a: number = randint(1, 10)\n'
              'let b: number = randrange(5)\n'
              'let c: number = randrange(1, 5)\n'
              'let d = choice([1, 2])\n'
              'let e: array = shuffle([1, 2])\n'
              'seed(42)\n'
              'let g: number = floor(1.5)\n'
              'let h: number = sin(0)\n')

    def test_string_methods_pass(self):
        check('let s: string = "hi".upper()\n'
              'let s2: string = "HI".lower()\n'
              'let s3: string = " x ".strip()\n'
              'let parts: array = "a,b".split(",")\n'
              'let parts2: array = "a b".split()\n'
              'let j: string = ",".join(["a", "b"])\n'
              'let c: bool = "hi".contains("h")\n'
              'let sw: bool = "hi".starts_with("h")\n'
              'let ew: bool = "hi".ends_with("i")\n'
              'let r: string = "hi".replace("h", "j")\n')

    def test_pi_and_e_are_numbers(self):
        check('let x: number = pi\nlet y: number = e + 1\n')

    def test_recursion_passes(self):
        check('func fib(n: number) -> number {\n'
              '    if n < 2 { return n }\n'
              '    return fib(n - 1) + fib(n - 2)\n'
              '}\nprint(fib(10))')

    def test_forward_call_passes(self):
        # A call before the definition still type-checks when the
        # argument types are right (top-level pre-pass).
        check('print(add(1, 2))\n'
              'func add(a: number, b: number) -> number '
              '{ return a + b }\n')

    def test_anonymous_func_let_binding_call(self):
        check('let double = func (n: number) -> number { return n * 2 }\n'
              'let r: number = double(21)\n')

    def test_match_valid(self):
        check('let x = 2\n'
              'let s: string = match x {\n'
              '    0 => "zero",\n'
              '    2 => "two",\n'
              '    _ => "?"\n'
              '}\nprint(s)')

    def test_match_binding_binds_scrutinee_type(self):
        check('let x: number = 5\n'
              'let y: number = match x { n => n + 1, _ => 0 }\n')

    def test_match_wildcard_ok(self):
        check('let s = match "x" { _ => 1 }\n'
              'let t = match 5 { _ => "?" }\n')

    def test_match_guard_ok(self):
        check('let s = match 3 { n if n > 1 => "big", _ => "small" }\n')

    def test_match_inference_all_arms_equal(self):
        check('let s: string = match 1 { 1 => "one", _ => "?" }\n')

    def test_match_inference_mixed_arms_is_any(self):
        # Mixed arm types infer "any", so any annotation accepts it.
        check('let v: any = match 1 { 1 => "one", _ => 2 }\n'
              'let w = match 1 { 1 => "one", _ => 2 }\n')

    def test_untyped_programs_pass(self):
        check('let x = 5\nlet y = "hi"\nprint(x)\nprint(y)\n')
        check('func f(a, b) { return a + b }\nprint(f("a", 1))\n')
        # Untyped but with a *known* bad condition still errors: the
        # rule only forgives "any", not a known number.
        check('if mystery { print("maybe") }\n')


class TestUntypedAndUnknownPass(unittest.TestCase):
    def test_undefined_variables_never_error(self):
        # The checker is type-only; undefined names are "any".
        check('print(nope)\nlet y: number = nope\nprint(add(1, "x"))\n')

    def test_arity_never_errors(self):
        check('func add(a: number, b: number) -> number '
              '{ return a + b }\nprint(add(1))\nprint(add(1, 2, 3))\n')

    def test_any_escapes_every_rule(self):
        check('let x = mystery()\n'
              'let n: number = x\n'
              'let s: string = x\n'
              'if x { print(1) }\n'
              'for i in x { print(i) }\n'
              'let r = x + 1\n'
              'let c = x < 2\n')


class TestLetRule(unittest.TestCase):
    def test_annotated_let_mismatch(self):
        check_err('let x: number = "hi"\n', "number")
        with self.assertRaises(KabirError) as ctx:
            check('let x: number = "hi"\n')
        self.assertIn("string", str(ctx.exception))

    def test_annotated_let_mismatch_bool(self):
        check_err('let x: string = true\n', "string")

    def test_annotated_let_mismatch_array(self):
        check_err('let x: array = 5\n', "array")


class TestParamAndReturnRules(unittest.TestCase):
    def test_call_arg_mismatch(self):
        check_err(
            'func add(a: number, b: number) -> number { return a + b }\n'
            'print(add(1, "x"))\n',
            "should be a number")

    def test_call_arg_mismatch_first_arg(self):
        check_err(
            'func greet(name: string) -> string { return name }\n'
            'print(greet(42))\n',
            "should be a string")

    def test_call_arg_unknown_is_ok(self):
        check('func add(a: number, b: number) -> number { return a + b }\n'
              'print(add(mystery, 2))\n')

    def test_return_mismatch(self):
        check_err('func f() -> number { return "hi" }\n',
                  "should return a number")

    def test_return_mismatch_bool(self):
        check_err('func f() -> string { return true }\n',
                  "should return a string")

    def test_bare_return_in_annotated_number_func(self):
        check_err('func f() -> number { return }\n',
                  "should return a number")

    def test_return_in_unannotated_func_never_errors(self):
        check('func f() { return "hi" }\n')

    def test_return_mismatch_in_nested_function(self):
        check_err(
            'func outer() -> number {\n'
            '    func inner() -> string { return 5 }\n'
            '    return 1\n'
            '}\n',
            "should return a string")


class TestArithmeticRules(unittest.TestCase):
    def test_subtract_string_errors(self):
        check_err('let n = "a" - 1\n', "needs numbers")

    def test_multiply_bool_errors(self):
        check_err('let n = true * 2\n', "needs numbers")

    def test_modulo_string_errors(self):
        check_err('let n = "a" % 2\n', "needs numbers")

    def test_power_string_errors(self):
        check_err('let n = "a" ** 2\n', "needs numbers")

    def test_plus_mixed_number_string_errors(self):
        check_err('let n = 1 + "a"\n', "Cannot add")

    def test_plus_mixed_string_array_errors(self):
        check_err('let n = "a" + [1]\n', "Cannot add")

    def test_plus_bool_errors(self):
        check_err('let n = true + 1\n', "Cannot add")

    def test_plus_unknown_is_ok(self):
        check('let n = mystery + 1\nlet s = mystery + "a"\n')


class TestComparisonRules(unittest.TestCase):
    def test_less_than_mixed_errors(self):
        check_err('let b = "a" < 1\n', "Cannot compare")

    def test_greater_than_mixed_errors(self):
        check_err('let b = 1 > "a"\n', "Cannot compare")

    def test_less_equal_bool_errors(self):
        check_err('let b = true <= false\n', "Cannot compare")

    def test_equality_never_errors(self):
        check('let b = 1 == "a"\nlet c = true != 5\n')


class TestConditionAndLoopRules(unittest.TestCase):
    def test_if_number_condition_errors(self):
        check_err('if 1 { print(1) }\n', "bool")

    def test_if_string_condition_errors(self):
        check_err('if "x" { print(1) }\n', "bool")

    def test_elif_condition_errors(self):
        check_err('if true { print(1) } elif 5 { print(2) }\n', "bool")

    def test_while_string_condition_errors(self):
        check_err('while "x" { break }\n', "bool")

    def test_for_over_number_errors(self):
        check_err('for x in 5 { print(x) }\n', "'for' needs")

    def test_for_over_bool_errors(self):
        check_err('for x in true { print(x) }\n', "'for' needs")

    def test_for_over_null_errors(self):
        check_err('for x in null { print(x) }\n', "'for' needs")


class TestMatchRules(unittest.TestCase):
    def test_literal_pattern_type_mismatch(self):
        check_err('let x = 5\n'
                  'let s = match x { "a" => 1, _ => 2 }\n',
                  "pattern is a string")

    def test_literal_pattern_bool_vs_number(self):
        check_err('let s = match 5 { true => 1, _ => 2 }\n',
                  "pattern is a bool")

    def test_literal_pattern_null_vs_string(self):
        check_err('let s = match "x" { null => 1, _ => 2 }\n',
                  "pattern is a null")

    def test_binding_arm_binds_scrutinee_type(self):
        # n binds a string here, so n + 1 is a type error.
        check_err('let s = "hi"\n'
                  'let r = match s { n => n + 1, _ => 0 }\n',
                  "Cannot add")

    def test_binding_arm_string_ok(self):
        check('let s = "hi"\n'
              'let r: string = match s { n => n + "!", _ => "?" }\n')

    def test_binding_does_not_leak(self):
        # n is unknown outside the match, so this passes the checker
        # (undefined variables are the runtime's job).
        check('let r = match 7 { n => n + 1 }\nprint(n)\n')

    def test_guard_must_be_bool(self):
        check_err('let s = match 5 { n if 1 => n, _ => 0 }\n',
                  "guard must be a bool")

    def test_match_scrutinee_unknown_no_error(self):
        check('let s = match mystery { "a" => 1, 2 => 3, _ => 4 }\n')


class TestUnknownTypeNames(unittest.TestCase):
    def test_unknown_let_type(self):
        err = check_err('let x: foo = 5\n',
                        'Unknown type "foo"')
        self.assertIn("use number, string, bool, array, dict, function, "
                      "or any", str(err))

    def test_unknown_param_type(self):
        check_err('func f(a: foo) { return a }\n',
                  'Unknown type "foo"')

    def test_unknown_return_type(self):
        check_err('func f() -> bar { return 1 }\n',
                  'Unknown type "bar"')

    def test_unknown_anonymous_func_type(self):
        check_err('let f = func (n: nope) { return n }\n',
                  'Unknown type "nope"')

    def test_null_literal_infers_null(self):
        # "null" is a type value (print returns it, null literals have
        # it) even though it cannot be written as an annotation: the
        # parser treats `null` as a keyword, not a type name.
        check('let x = null\nlet y = print("hi")\n')


class TestBuiltinSignatures(unittest.TestCase):
    def test_len_number_errors(self):
        check_err('let n = len(5)\n', "len() works")

    def test_len_bool_errors(self):
        check_err('let n = len(true)\n', "len() works")

    def test_num_bool_errors(self):
        check_err('let n = num(true)\n', "num() works")

    def test_input_number_errors(self):
        check_err('let s = input(5)\n', "input() needs")

    def test_range_string_errors(self):
        check_err('let r = range("x")\n', "range() needs")

    def test_push_non_array_errors(self):
        check_err('let a = push(5, 1)\n', "push() needs")

    def test_pop_non_array_errors(self):
        check_err('let v = pop("hi")\n', "pop() needs")

    def test_sqrt_string_errors(self):
        check_err('let n = sqrt("x")\n', "sqrt() needs")

    def test_pow_string_errors(self):
        check_err('let n = pow(2, "x")\n', "pow() needs")

    def test_keys_array_errors(self):
        check_err('let k = keys([1])\n', "keys() needs")

    def test_int_bool_errors(self):
        check_err('let n = int(true)\n', "int() works")

    def test_randint_string_errors(self):
        check_err('let n = randint("a", 2)\n', "randint() needs")

    def test_choice_non_array_errors(self):
        check_err('let n = choice(5)\n', "choice() needs")

    def test_seed_string_errors(self):
        check_err('seed("x")\n', "seed() needs")

    def test_floor_string_errors(self):
        check_err('let n = floor("x")\n', "floor() needs")

    def test_join_non_array_errors(self):
        check_err('let s = ",".join("x")\n', "needs array")

    def test_split_non_string_sep_errors(self):
        check_err('let p = "a,b".split(5)\n', "needs string")

    def test_contains_non_string_errors(self):
        check_err('let b = "hi".contains(5)\n', "needs string")

    def test_string_method_on_number_errors(self):
        check_err('let s = (5).upper()\n', "works on a string")

    def test_builtin_arg_unknown_is_ok(self):
        check('let n = len(mystery)\nlet r = range(mystery)\n')


class TestFirstErrorAndApi(unittest.TestCase):
    def test_returns_none_on_success(self):
        self.assertIsNone(check('let x: number = 5\n'))

    def test_first_error_wins(self):
        with self.assertRaises(KabirError) as ctx:
            check('let x: number = "a"\nlet y: string = 5\n')
        self.assertIn("number", str(ctx.exception))
        self.assertNotIn("y", str(ctx.exception))

    def test_error_carries_line(self):
        with self.assertRaises(KabirError) as ctx:
            check('let ok = 1\nlet x: number = "bad"\n')
        self.assertEqual(ctx.exception.line, 2)

    def test_check_program_accepts_defaults(self):
        program = parse('let x: number = "bad"\n')
        with self.assertRaises(KabirError):
            check_program(program)


class TestExamplesPass(unittest.TestCase):
    def test_all_examples_pass(self):
        examples = sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "examples", "*.kbr")))
        self.assertEqual(len(examples), 11)
        for path in examples:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            with self.subTest(example=os.path.basename(path)):
                check(source)


if __name__ == "__main__":
    unittest.main()
