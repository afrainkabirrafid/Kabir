"""Shared runtime semantics for Kabir.

Pure functions used by BOTH the tree-walking interpreter and the
bytecode VM so behaviour cannot drift. Each function raises
``KabirRuntimeError(message)`` (a plain ``Exception`` subclass,
message only, no position); callers convert it to a ``KabirError``
with node/instruction position.

The logic here is moved verbatim from ``kabir/interpreter.py``
(``_binary``, ``is_truthy``, ``format_value``, ``kabir_equal``),
including the exact error message strings.
"""

from .builtins import BuiltinFunction, _type_name


class KabirRuntimeError(Exception):
    """A runtime failure with a message only (no position)."""


def _is_function(value):
    """True for a user-defined Kabir function value (interpreter or VM)."""
    if isinstance(value, BuiltinFunction):
        return True
    return (hasattr(value, "closure_env") and hasattr(value, "params")
            and hasattr(value, "body"))


def is_truthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def format_value(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join('"%s": %s' % (k, format_value(v))
                          for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, BuiltinFunction):
        return "<builtin function %s>" % value.name
    if _is_function(value):
        return "<function %s>" % value.name
    return str(value)


def kabir_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) \
            and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            kabir_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return (set(left.keys()) == set(right.keys())
                and all(kabir_equal(left[k], right[k])
                        for k in left))
    if _is_function(left) or _is_function(right):
        return left is right
    # 4.0.0 async values (tasks, channels) compare by identity:
    # two handles to the same task/channel are equal, anything else
    # is not. They name themselves through _kabir_type (see
    # kabir/builtins.py) so this module never imports the VM.
    if (getattr(left, "_kabir_type", None) is not None
            or getattr(right, "_kabir_type", None) is not None):
        return left is right
    return False


def binary_op(op, left, right):
    """Apply a binary operator; raises KabirRuntimeError on bad operands."""
    if op == "==":
        return kabir_equal(left, right)
    if op == "!=":
        return not kabir_equal(left, right)
    if op in ("<", "<=", ">", ">="):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                and not isinstance(left, bool) and not isinstance(right, bool):
            pass
        elif isinstance(left, str) and isinstance(right, str):
            pass
        else:
            raise KabirRuntimeError(
                "Cannot compare %s with %s using '%s' -- "
                "compare numbers with numbers, or strings with "
                "strings" % (_type_name(left), _type_name(right), op))
        return {"<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]
    if op == "+":
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                and not isinstance(left, bool) and not isinstance(right, bool):
            return left + right
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, list) and isinstance(right, list):
            return left + right
        raise KabirRuntimeError(
            "Cannot add %s and %s -- '+' works on two "
            "numbers, two strings, or two arrays"
            % (_type_name(left), _type_name(right)))
    if op in ("-", "*", "/", "%", "**"):
        if not (isinstance(left, (int, float)) and not isinstance(left, bool)) \
                or not (isinstance(right, (int, float)) and not isinstance(right, bool)):
            raise KabirRuntimeError(
                "'%s' needs two numbers, but got %s and %s"
                % (op, _type_name(left), _type_name(right)))
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if right == 0:
                raise KabirRuntimeError(
                    "Division by zero -- the number after "
                    "'/' cannot be 0")
            if isinstance(left, int) and isinstance(right, int) \
                    and left % right == 0:
                return left // right
            return left / right
        if op == "%":
            if right == 0:
                raise KabirRuntimeError(
                    "Division by zero -- the number after "
                    "'%' cannot be 0")
            return left % right
        if op == "**":
            try:
                return left ** right
            except (OverflowError, ZeroDivisionError):
                raise KabirRuntimeError(
                    "That power is too large to calculate")
    raise KabirRuntimeError("Unknown operator %r" % op)
