"""Standard library for Kabir.

Plain global built-ins plus the string-method table. Implementations
are plain Python functions following the same conventions as
kabir/builtins.py (arg counts via _need, type names via _type_name,
errors via interp.error). This module must not import the interpreter
(no circular import).
"""

import json
import math
import os
import random
import time
import urllib.error
import urllib.request

from .builtins import BuiltinFunction, _is_number, _need, _type_name


# --- String methods ---------------------------------------------------
# Each method is (interp, node, receiver, args) -> value, where
# receiver is the string the method was called on.

def _str_upper(interp, node, receiver, args):
    _need(interp, node, args, "upper", 0)
    return receiver.upper()


def _str_lower(interp, node, receiver, args):
    _need(interp, node, args, "lower", 0)
    return receiver.lower()


def _str_strip(interp, node, receiver, args):
    _need(interp, node, args, "strip", 0)
    return receiver.strip()


def _str_split(interp, node, receiver, args):
    if len(args) not in (0, 1):
        interp.error(node, "split() takes 0 or 1 arguments, but got %d"
                     % len(args))
    if not args:
        return receiver.split()
    sep = args[0]
    if not isinstance(sep, str):
        interp.error(node, "split() needs a string separator, not %s"
                     % _type_name(sep))
    if sep == "":
        interp.error(node, "split() needs a non-empty separator")
    return receiver.split(sep)


def _str_join(interp, node, receiver, args):
    _need(interp, node, args, "join", 1)
    arr = args[0]
    if not isinstance(arr, list):
        interp.error(node, "join() needs an array, not %s"
                     % _type_name(arr))
    for element in arr:
        if not isinstance(element, str):
            interp.error(node, "join() needs an array of strings, not "
                         "%s" % _type_name(element))
    return receiver.join(arr)


def _str_contains(interp, node, receiver, args):
    _need(interp, node, args, "contains", 1)
    sub = args[0]
    if not isinstance(sub, str):
        interp.error(node, "contains() needs a string, not %s"
                     % _type_name(sub))
    return sub in receiver


def _str_starts_with(interp, node, receiver, args):
    _need(interp, node, args, "starts_with", 1)
    prefix = args[0]
    if not isinstance(prefix, str):
        interp.error(node, "starts_with() needs a string, not %s"
                     % _type_name(prefix))
    return receiver.startswith(prefix)


def _str_ends_with(interp, node, receiver, args):
    _need(interp, node, args, "ends_with", 1)
    suffix = args[0]
    if not isinstance(suffix, str):
        interp.error(node, "ends_with() needs a string, not %s"
                     % _type_name(suffix))
    return receiver.endswith(suffix)


def _str_replace(interp, node, receiver, args):
    _need(interp, node, args, "replace", 2)
    old, new = args
    if not isinstance(old, str):
        interp.error(node, "replace() needs strings, not %s"
                     % _type_name(old))
    if not isinstance(new, str):
        interp.error(node, "replace() needs strings, not %s"
                     % _type_name(new))
    return receiver.replace(old, new)


STRING_METHODS = {
    "upper": _str_upper,
    "lower": _str_lower,
    "strip": _str_strip,
    "split": _str_split,
    "join": _str_join,
    "contains": _str_contains,
    "starts_with": _str_starts_with,
    "ends_with": _str_ends_with,
    "replace": _str_replace,
}


# --- random -----------------------------------------------------------
# All five functions use the per-interpreter RNG on interp._random,
# so two Interpreter instances (and therefore tests) are independent.

def _whole(interp, node, value, name):
    """Return int(value) if value is a whole number, else raise KabirError."""
    if not _is_number(value) or int(value) != value:
        interp.error(node, "%s needs whole numbers, not %s"
                     % (name, interp.format_value(value)))
    return int(value)


def _stdlib_randint(interp, node, args):
    _need(interp, node, args, "randint", 2)
    a = _whole(interp, node, args[0], "randint")
    b = _whole(interp, node, args[1], "randint")
    if a > b:
        interp.error(node, "randint() needs the first number to be no "
                     "greater than the second, but got %d and %d" % (a, b))
    return interp._random.randint(a, b)


def _stdlib_randrange(interp, node, args):
    if len(args) not in (1, 2):
        interp.error(node, "randrange() takes 1 or 2 arguments, but got %d"
                     % len(args))
    nums = [_whole(interp, node, a, "randrange") for a in args]
    if len(nums) == 1:
        if nums[0] <= 0:
            interp.error(node, "randrange() needs a number greater than 0, "
                         "but got %d" % nums[0])
        return interp._random.randrange(nums[0])
    if nums[0] >= nums[1]:
        interp.error(node, "randrange() needs a non-empty range -- the "
                     "first number must be less than the second, but got "
                     "%d and %d" % (nums[0], nums[1]))
    return interp._random.randrange(nums[0], nums[1])


def _stdlib_choice(interp, node, args):
    _need(interp, node, args, "choice", 1)
    if not isinstance(args[0], list):
        interp.error(node, "choice() needs an array, not %s"
                     % _type_name(args[0]))
    if not args[0]:
        interp.error(node, "choice() cannot choose from an empty array")
    return interp._random.choice(args[0])


def _stdlib_shuffle(interp, node, args):
    _need(interp, node, args, "shuffle", 1)
    if not isinstance(args[0], list):
        interp.error(node, "shuffle() needs an array, not %s"
                     % _type_name(args[0]))
    interp._random.shuffle(args[0])
    return args[0]


def _stdlib_seed(interp, node, args):
    _need(interp, node, args, "seed", 1)
    n = _whole(interp, node, args[0], "seed")
    interp._random.seed(n)
    return None


# --- math -------------------------------------------------------------
# sqrt/pow/abs stay in kabir/builtins.py; these seven join them as
# plain globals, documented together in docs/LANGUAGE.md.

def _stdlib_floor(interp, node, args):
    _need(interp, node, args, "floor", 1)
    if not _is_number(args[0]):
        interp.error(node, "floor() needs a number, not %s"
                     % _type_name(args[0]))
    return math.floor(args[0])


def _stdlib_ceil(interp, node, args):
    _need(interp, node, args, "ceil", 1)
    if not _is_number(args[0]):
        interp.error(node, "ceil() needs a number, not %s"
                     % _type_name(args[0]))
    return math.ceil(args[0])


def _stdlib_sin(interp, node, args):
    _need(interp, node, args, "sin", 1)
    if not _is_number(args[0]):
        interp.error(node, "sin() needs a number, not %s"
                     % _type_name(args[0]))
    return math.sin(args[0])


def _stdlib_cos(interp, node, args):
    _need(interp, node, args, "cos", 1)
    if not _is_number(args[0]):
        interp.error(node, "cos() needs a number, not %s"
                     % _type_name(args[0]))
    return math.cos(args[0])


def _stdlib_tan(interp, node, args):
    _need(interp, node, args, "tan", 1)
    if not _is_number(args[0]):
        interp.error(node, "tan() needs a number, not %s"
                     % _type_name(args[0]))
    return math.tan(args[0])


def _stdlib_log(interp, node, args):
    _need(interp, node, args, "log", 1)
    if not _is_number(args[0]):
        interp.error(node, "log() needs a number, not %s"
                     % _type_name(args[0]))
    if args[0] <= 0:
        interp.error(node, "log() needs a number greater than 0, not %s"
                     % interp.format_value(args[0]))
    return math.log(args[0])


def _stdlib_exp(interp, node, args):
    _need(interp, node, args, "exp", 1)
    if not _is_number(args[0]):
        interp.error(node, "exp() needs a number, not %s"
                     % _type_name(args[0]))
    try:
        return math.exp(args[0])
    except OverflowError:
        interp.error(node, "That power is too large to calculate")


# --- file I/O ---------------------------------------------------------
# Paths are plain strings. Every failure is a friendly KabirError
# (missing file, directory, permission, bad UTF-8) -- never a Python
# traceback. file_exists() reports False for a directory: it answers
# "is there a file here", not "does this path exist".

def _stdlib_read_file(interp, node, args):
    _need(interp, node, args, "read_file", 1)
    path = args[0]
    if not isinstance(path, str):
        interp.error(node, "read_file() needs a string path, not %s"
                     % _type_name(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        interp.error(node, "Cannot read file %r -- no such file" % path)
    except IsADirectoryError:
        interp.error(node, "Cannot read file %r -- it is a directory"
                     % path)
    except PermissionError:
        interp.error(node, "Cannot read file %r -- permission denied"
                     % path)
    except UnicodeDecodeError:
        interp.error(node, "Cannot read file %r -- it is not valid "
                     "UTF-8 text" % path)
    except OSError as exc:
        interp.error(node, "Cannot read file %r -- %s"
                     % (path, exc.strerror or exc))


def _stdlib_write_file(interp, node, args):
    _need(interp, node, args, "write_file", 2)
    path, text = args
    if not isinstance(path, str):
        interp.error(node, "write_file() needs a string path, not %s"
                     % _type_name(path))
    if not isinstance(text, str):
        interp.error(node, "write_file() needs a string to write, not %s"
                     % _type_name(text))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except IsADirectoryError:
        interp.error(node, "Cannot write file %r -- it is a directory"
                     % path)
    except PermissionError:
        interp.error(node, "Cannot write file %r -- permission denied"
                     % path)
    except OSError as exc:
        interp.error(node, "Cannot write file %r -- %s"
                     % (path, exc.strerror or exc))
    return None


def _stdlib_file_exists(interp, node, args):
    _need(interp, node, args, "file_exists", 1)
    path = args[0]
    if not isinstance(path, str):
        interp.error(node, "file_exists() needs a string path, not %s"
                     % _type_name(path))
    return os.path.isfile(path)


# --- JSON -------------------------------------------------------------
# json_parse maps JSON null/bool/number/string/array/object onto
# Kabir null/bool/number/string/array/dict. json_stringify is the
# reverse; values JSON cannot hold (functions) raise a KabirError.

def _stdlib_json_parse(interp, node, args):
    _need(interp, node, args, "json_parse", 1)
    text = args[0]
    if not isinstance(text, str):
        interp.error(node, "json_parse() needs a string, not %s"
                     % _type_name(text))
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        interp.error(node, "Cannot parse JSON -- %s (line %d, column %d)"
                     % (exc.msg, exc.lineno, exc.colno))


def _stdlib_json_stringify(interp, node, args):
    _need(interp, node, args, "json_stringify", 1)
    try:
        return json.dumps(args[0])
    except (TypeError, ValueError) as exc:
        interp.error(node, "Cannot turn %s into JSON -- %s"
                     % (interp.format_value(args[0]), exc))


# --- HTTP -------------------------------------------------------------
# stdlib urllib only, 10-second timeout. The body is always a string
# (decoded as UTF-8, invalid bytes replaced). HTTP error statuses and
# network failures both raise a friendly KabirError.

_HTTP_TIMEOUT = 10


def _stdlib_http_get(interp, node, args):
    _need(interp, node, args, "http_get", 1)
    url = args[0]
    if not isinstance(url, str):
        interp.error(node, "http_get() needs a string URL, not %s"
                     % _type_name(url))
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        interp.error(node, "http_get(%r) failed -- the server returned "
                     "HTTP %d" % (url, exc.code))
    except urllib.error.URLError as exc:
        interp.error(node, "http_get(%r) failed -- %s" % (url, exc.reason))
    except (OSError, ValueError) as exc:
        interp.error(node, "http_get(%r) failed -- %s" % (url, exc))


def _stdlib_http_post(interp, node, args):
    _need(interp, node, args, "http_post", 2)
    url, body = args
    if not isinstance(url, str):
        interp.error(node, "http_post() needs a string URL, not %s"
                     % _type_name(url))
    if not isinstance(body, str):
        interp.error(node, "http_post() needs a string body, not %s"
                     % _type_name(body))
    request = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(request,
                                    timeout=_HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        interp.error(node, "http_post(%r) failed -- the server returned "
                     "HTTP %d" % (url, exc.code))
    except urllib.error.URLError as exc:
        interp.error(node, "http_post(%r) failed -- %s" % (url, exc.reason))
    except (OSError, ValueError) as exc:
        interp.error(node, "http_post(%r) failed -- %s" % (url, exc))


# --- time -------------------------------------------------------------

def _stdlib_now(interp, node, args):
    _need(interp, node, args, "now", 0)
    return time.time()


def _stdlib_sleep(interp, node, args):
    _need(interp, node, args, "sleep", 1)
    seconds = args[0]
    if not _is_number(seconds):
        interp.error(node, "sleep() needs a number of seconds, not %s"
                     % _type_name(seconds))
    if seconds < 0:
        interp.error(node, "sleep() needs a number of seconds no less "
                     "than 0, but got %s" % interp.format_value(seconds))
    time.sleep(seconds)
    return None


STDLIB = {
    "randint": _stdlib_randint,
    "randrange": _stdlib_randrange,
    "choice": _stdlib_choice,
    "shuffle": _stdlib_shuffle,
    "seed": _stdlib_seed,
    "floor": _stdlib_floor,
    "ceil": _stdlib_ceil,
    "sin": _stdlib_sin,
    "cos": _stdlib_cos,
    "tan": _stdlib_tan,
    "log": _stdlib_log,
    "exp": _stdlib_exp,
    "read_file": _stdlib_read_file,
    "write_file": _stdlib_write_file,
    "file_exists": _stdlib_file_exists,
    "json_parse": _stdlib_json_parse,
    "json_stringify": _stdlib_json_stringify,
    "http_get": _stdlib_http_get,
    "http_post": _stdlib_http_post,
    "now": _stdlib_now,
    "sleep": _stdlib_sleep,
}


def build_stdlib():
    """Return {name: BuiltinFunction} for a fresh interpreter."""
    return {name: BuiltinFunction(name, impl)
            for name, impl in STDLIB.items()}
