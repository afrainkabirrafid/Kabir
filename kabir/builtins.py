"""Built-in functions for Kabir.

Each built-in is a plain Python function taking (interp, node, args)
and returning a Kabir value (Python int/float/str/bool/None/list/dict,
a KabirFunction, or a BuiltinFunction). Errors are raised through
interp.error() so they carry the friendly file+line+caret format.
"""

import math


class BuiltinFunction:
    """A Kabir built-in function value."""

    def __init__(self, name, impl):
        self.name = name
        self.impl = impl

    def __repr__(self):
        return "<builtin function %s>" % self.name


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "dict"
    return "function"


def _need(interp, node, args, name, count):
    if len(args) != count:
        interp.error(node, "%s() takes exactly %d argument%s, but got %d"
                     % (name, count, "" if count == 1 else "s", len(args)))


def _builtin_print(interp, node, args):
    print(" ".join(interp.format_value(a) for a in args))
    return None


def _builtin_input(interp, node, args):
    if len(args) > 1:
        interp.error(node, "input() takes at most 1 argument, but got %d"
                     % len(args))
    prompt = ""
    if args:
        if not isinstance(args[0], str):
            interp.error(node, "input() prompt must be a string, not %s"
                         % _type_name(args[0]))
        prompt = args[0]
    try:
        return input(prompt)
    except EOFError:
        interp.error(node, "No more input -- the input stream ended")
    except KeyboardInterrupt:
        interp.error(node, "Input was interrupted")


def _builtin_len(interp, node, args):
    _need(interp, node, args, "len", 1)
    v = args[0]
    if isinstance(v, (str, list, dict)):
        return len(v)
    interp.error(node, "len() works on a string, array or dict, not %s"
                 % _type_name(v))


def _builtin_str(interp, node, args):
    _need(interp, node, args, "str", 1)
    return interp.format_value(args[0])


def _builtin_num(interp, node, args):
    _need(interp, node, args, "num", 1)
    v = args[0]
    if _is_number(v):
        return v
    if isinstance(v, str):
        text = v.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        interp.error(node, 'Cannot turn %r into a number' % v)
    interp.error(node, "num() works on a string or a number, not %s"
                 % _type_name(v))


def _builtin_type(interp, node, args):
    _need(interp, node, args, "type", 1)
    return _type_name(args[0])


def _builtin_range(interp, node, args):
    if len(args) not in (1, 2):
        interp.error(node, "range() takes 1 or 2 arguments, but got %d"
                     % len(args))
    for a in args:
        if not _is_number(a) or int(a) != a:
            interp.error(node, "range() needs whole numbers, not %s"
                         % interp.format_value(a))
    if len(args) == 1:
        return list(range(int(args[0])))
    return list(range(int(args[0]), int(args[1])))


def _builtin_push(interp, node, args):
    _need(interp, node, args, "push", 2)
    if not isinstance(args[0], list):
        interp.error(node, "push() needs an array first, not %s"
                     % _type_name(args[0]))
    args[0].append(args[1])
    return args[0]


def _builtin_pop(interp, node, args):
    _need(interp, node, args, "pop", 1)
    if not isinstance(args[0], list):
        interp.error(node, "pop() needs an array, not %s"
                     % _type_name(args[0]))
    if not args[0]:
        interp.error(node, "pop() cannot remove from an empty array")
    return args[0].pop()


def _builtin_sqrt(interp, node, args):
    _need(interp, node, args, "sqrt", 1)
    if not _is_number(args[0]):
        interp.error(node, "sqrt() needs a number, not %s"
                     % _type_name(args[0]))
    if args[0] < 0:
        interp.error(node, "sqrt() cannot take the square root of a "
                     "negative number")
    return math.sqrt(args[0])


def _builtin_pow(interp, node, args):
    _need(interp, node, args, "pow", 2)
    if not _is_number(args[0]) or not _is_number(args[1]):
        interp.error(node, "pow() needs two numbers")
    return args[0] ** args[1]


def _builtin_abs(interp, node, args):
    _need(interp, node, args, "abs", 1)
    if not _is_number(args[0]):
        interp.error(node, "abs() needs a number, not %s"
                     % _type_name(args[0]))
    return abs(args[0])


def _builtin_keys(interp, node, args):
    _need(interp, node, args, "keys", 1)
    if not isinstance(args[0], dict):
        interp.error(node, "keys() needs a dict, not %s"
                     % _type_name(args[0]))
    return list(args[0].keys())


def _builtin_int(interp, node, args):
    _need(interp, node, args, "int", 1)
    v = args[0]
    if isinstance(v, bool):
        interp.error(node, "int() cannot convert a boolean")
    if _is_number(v):
        return int(v)
    if isinstance(v, str):
        text = v.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return int(float(text))
        except ValueError:
            pass
        interp.error(node, 'Cannot turn %r into an integer' % v)
    interp.error(node, "int() works on a number or a string, not %s"
                 % _type_name(v))


def _builtin_float(interp, node, args):
    _need(interp, node, args, "float", 1)
    v = args[0]
    if isinstance(v, bool):
        interp.error(node, "float() cannot convert a boolean")
    if _is_number(v):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            interp.error(node, 'Cannot turn %r into a float' % v)
    interp.error(node, "float() works on a number or a string, not %s"
                 % _type_name(v))


BUILTINS = {
    "print": _builtin_print,
    "input": _builtin_input,
    "len": _builtin_len,
    "str": _builtin_str,
    "num": _builtin_num,
    "type": _builtin_type,
    "range": _builtin_range,
    "push": _builtin_push,
    "pop": _builtin_pop,
    "sqrt": _builtin_sqrt,
    "pow": _builtin_pow,
    "abs": _builtin_abs,
    "keys": _builtin_keys,
    "int": _builtin_int,
    "float": _builtin_float,
}


def build_builtins():
    """Return {name: BuiltinFunction} for a fresh interpreter."""
    return {name: BuiltinFunction(name, impl)
            for name, impl in BUILTINS.items()}
