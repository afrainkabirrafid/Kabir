"""Opt-in static type checker for Kabir 2.0.0.

``check_program(program, filename="<stdin>", lines=None)`` walks the AST
and raises ``KabirError`` (with file/line) on the FIRST type error, or
returns ``None`` when the program type-checks.

Types are plain strings: ``"number"``, ``"string"``, ``"bool"``,
``"array"``, ``"dict"``, ``"function"``, ``"any"``, ``"null"``.

The checker is TYPE-ONLY: it never errors on undefined variables,
arity, or runtime errors -- the runtime owns those. Every rule errors
ONLY when the involved types are statically known (i.e. not ``"any"``),
so untyped programs always pass.

3.0.0 ownership lite (opt-in, checker only; the VM/interpreter are
unchanged and the REPL runs without the checker, so use-after-move is
not enforced there):

- ``let mut x = <array|dict>`` makes ``x`` an OWNED binding.
  Assigning ``x`` to another binding, passing it as an argument, or
  returning it MOVES ``x``; any later use of ``x`` is a compile-time
  error ("used after move"). Numbers/strings/bools/null are Copy --
  moves never apply to them. ``let mut`` on a Copy value is allowed
  and is a no-op marker.
- Plain ``let`` bindings are never move-tracked.
- Reassignment (``x = ...``) to a moved-from binding re-initializes
  it: it is live again (owned again if the new value is an array or
  dict, un-owned if the new value is a Copy value).
- No ``mut`` on function params in 3.0.0.
- Move tracking is flow-insensitive and scope-based: a move inside a
  conditional block or a nested function still marks the binding
  moved afterwards. Only a bare variable as the whole moved value
  moves it (``let y = x``, ``f(x)``, ``return x``); uses inside a
  larger expression (``x[0]``, ``x + [1]``) do not -- except that
  passing ``x`` as a call argument moves it. (In 3.0.0-5.0.0 that
  included ``print(x)`` and ``len(x)``; since 6.0.0 read-only
  builtins borrow instead -- see below.)
  A binding whose type is statically unknown ("any") is never
  tracked; an explicit ``: array`` / ``: dict`` annotation makes a
  ``let mut`` binding owned even when its value's type is unknown.

6.0.0 changes (both breaking, ordered by the user -- "do all"):

- Read-only builtins borrow, they do not move: ``print``, ``len``,
  ``str``, ``num``, ``type``, ``range``, ``sqrt``, ``pow``, ``abs``,
  ``keys``, ``int``, ``float`` (and ``input``), plus the read-only
  stdlib functions ``choice`` and ``json_stringify``, take a SHARED
  BORROW of their arguments for the duration of the call -- passing
  an owned array/dict to one of them no longer moves it, so the
  value stays usable afterwards. Everything else still moves:
  assignment, user-function calls, ``return``, and the consuming /
  mutating builtins ``push``, ``pop`` and ``send``.
- Immutable by default: plain ``let`` is now immutable. Assigning
  to a plain-``let`` binding (``x = ...``, ``x += ...``, ``x -= ...``)
  is a compile-time error; ``let mut`` is the mutable form.
  Function parameters, ``for`` loop variables, and ``match`` bindings
  are immutable too (they were never ``let mut``). Index assignment
  (``a[i] = ...``) and ``push``/``pop`` mutate a value's CONTENTS,
  not the binding itself, so they are still allowed on a plain
  ``let`` -- only rebinding the name is forbidden. The REPL and
  ``kabir run --interp`` run without the checker, so they do not
  enforce either rule (as with all ownership rules before).

4.0.0 borrow checker (honest subset -- lifetime elision only, NO
explicit ``'a`` lifetime parameters):

- ``&x`` is a shared borrow, ``&mut x`` a mutable borrow. A borrow
  is a compile-time discipline only: at runtime it IS the value
  (``&x`` compiles to just ``x``), so borrowing costs nothing and
  the REPL (which runs without the checker) does not enforce it.
- While a mutable borrow of a place is live: no other access to
  that place (no read, write, or move). While shared borrows of a
  place are live: no mutation or move of that place, reads are fine.
- A borrowed place cannot be moved while borrowed (owned bindings
  only -- plain ``let`` bindings never move, so the rule only bites
  for ``let mut``). ``&mut x`` requires ``x`` to be a ``let mut``
  binding; borrowing a moved value is an error.
- NLL-lite: a borrow ends at its last use. Last use is computed by
  a simple walk over each statement list: a borrow held by ``r``
  lives from its creation until the last statement in the same block
  that mentions ``r``. An unused borrow ends at its own statement.
- Borrows of ``a[i]`` borrow the whole container. A borrow stored
  in a binding (``let r = &x``, or ``let a = [&x]``) is tracked
  through that binding; a borrow passed as a call argument or
  returned lives only for that statement.
- Lifetime elision only: a function whose return type is a borrow
  (``-> &number``) must take exactly one borrowed parameter, else
  a clear error. Returning a borrow of a local is an error -- only
  a borrow of a borrowed parameter (or of a top-level binding) may
  be returned. NO explicit lifetime parameters.
- Borrow types are strings: ``"&number"``, ``"&mut array"``.
  ``&mut T`` coerces to ``&T`` where a ``&T`` is expected; a borrow
  never coerces to the plain type (no auto-deref): ``len(&x)`` is a
  type error, pass the original binding instead.

Documented limits (see also docs/LANGUAGE.md): no lifetime params;
no reborrowing (borrowing a borrow-typed value is an error);
mutable reborrowing of a borrowed parameter is not supported
(params cannot be ``let mut``); borrows are tracked within one
function body (or the top level) -- a top-level borrow does not
constrain code inside a function body, and a borrow inside a nested
function never escapes it; no interprocedural tracking beyond
signatures (a function called with a borrowed place may mutate it
through the alias); ``push``/``pop`` are the only builtins treated
as mutating their array argument; last-use is flow-insensitive
(a use inside a branch that never runs still extends the borrow).
"""

from . import ast_nodes as ast
from .errors import KabirError, error_at

VALID_TYPES = frozenset(
    ("number", "string", "bool", "array", "dict", "function", "any", "null")
)

UNKNOWN_TYPE_MSG = (
    'Unknown type "%s" -- use number, string, bool, array, dict, '
    "function, or any"
)

# Builtin / stdlib signatures: name -> return type. Argument checking
# lives in _check_builtin_call / _check_stdlib_call below, because
# several take optional or variadic arguments.
_BUILTIN_RETURNS = {
    "print": "null",
    "input": "string",
    "len": "number",
    "str": "string",
    "num": "number",
    "type": "string",
    "range": "array",
    "push": "array",
    "pop": "any",
    "sqrt": "number",
    "pow": "number",
    "abs": "number",
    "keys": "array",
    "int": "number",
    "float": "number",
}

_STDLIB_RETURNS = {
    "randint": "number",
    "randrange": "number",
    "choice": "any",
    "shuffle": "array",
    "seed": "null",
    "floor": "number",
    "ceil": "number",
    "sin": "number",
    "cos": "number",
    "tan": "number",
    "log": "number",
    "exp": "number",
    # 3.0.0 stdlib expansion (Wave 4). The per-name branches in
    # _check_stdlib_call enforce the argument types below; the dict
    # alone is not enough (the old fallback branch demands a number).
    "read_file": "string",
    "write_file": "null",
    "file_exists": "bool",
    "json_parse": "any",
    "json_stringify": "string",
    "http_get": "string",
    "http_post": "string",
    "now": "number",
    "sleep": "null",
}

# 6.0.0: read-only builtins / stdlib functions. These take a SHARED
# BORROW of their arguments (for the call only) and never move an
# owned value. Everything not listed here moves a bare owned variable
# passed to it: user functions, `push` / `pop` (they mutate the
# array in place), and `send` (the value goes into the channel).
# `spawn` / `recv` / `chan` take no owned array/dict, so they behave
# the same either way and stay on the moving path.
_READ_ONLY_BUILTINS = frozenset((
    "print", "input", "len", "str", "num", "type", "range",
    "sqrt", "pow", "abs", "keys", "int", "float",
))

_READ_ONLY_STDLIB = frozenset((
    "choice", "json_stringify",
))

# String methods: name -> (arg-type sets per position, return type).
# An empty arg list means the method takes no arguments.
_STRING_METHODS = {
    "upper": ([], "string"),
    "lower": ([], "string"),
    "strip": ([], "string"),
    "split": ([{"string"}], "array"),   # separator is optional
    "join": ([{"array"}], "string"),
    "contains": ([{"string"}], "bool"),
    "starts_with": ([{"string"}], "bool"),
    "ends_with": ([{"string"}], "bool"),
    "replace": ([{"string"}, {"string"}], "string"),
}


def _literal_type(value):
    """The Kabir type of a literal Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "any"


def _borrow_holder_names(node):
    """Variable names read by *node* that could carry a borrow into
    a new binding (for _extend_borrow_holders).

    Skips nested function bodies (their borrows belong to them) and
    ``match`` binding patterns (definitions, not reads)."""
    names = set()
    _collect_holder_names(node, names)
    return names


def _collect_holder_names(node, names):
    if node is None:
        return
    if isinstance(node, ast.FuncDecl):
        return
    if isinstance(node, ast.Var):
        names.add(node.name)
        return
    if isinstance(node, ast.MatchExpr):
        _collect_holder_names(node.scrutinee, names)
        for arm in node.arms:
            # arm.pattern binds a name; it is not a read of it.
            if arm.guard is not None:
                _collect_holder_names(arm.guard, names)
            _collect_holder_names(arm.body, names)
        return
    if isinstance(node, ast.Node):
        for value in vars(node).values():
            if isinstance(value, ast.Node):
                _collect_holder_names(value, names)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, ast.Node):
                        _collect_holder_names(item, names)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, ast.Node):
                                _collect_holder_names(part, names)


def _last_uses(statements):
    """Map each variable name to the index of the last statement in
    *statements* that mentions it (NLL-lite liveness).

    A "mention" is any ``Var`` node anywhere in the statement,
    including inside nested blocks and nested function bodies (a
    capture counts as a use at the statement that creates the
    function -- conservative). A ``match`` binding pattern is a
    definition, not a use, so it is skipped; the arm body still
    counts. Interpolation parts are source text, so they are parsed
    best-effort and walked too.
    """
    last = {}
    for index, stmt in enumerate(statements):
        for name in _names_used(stmt):
            last[name] = index
    return last


def _names_used(node):
    names = set()
    _collect_names(node, names)
    return names


def _collect_names(node, names):
    if node is None:
        return
    if isinstance(node, ast.Var):
        names.add(node.name)
        return
    if isinstance(node, ast.MatchExpr):
        _collect_names(node.scrutinee, names)
        for arm in node.arms:
            # arm.pattern binds a name; it is not a use of it.
            if arm.guard is not None:
                _collect_names(arm.guard, names)
            _collect_names(arm.body, names)
        return
    if isinstance(node, ast.StringLiteral):
        for kind, text in node.parts:
            if kind != "expr":
                continue
            try:
                from .parser import parse as _parse
                inner = _parse(text, "<interpolation>")
            except Exception:
                continue
            for stmt in inner.statements:
                _collect_names(stmt, names)
        return
    if isinstance(node, ast.Node):
        for value in vars(node).values():
            if isinstance(value, ast.Node):
                _collect_names(value, names)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, ast.Node):
                        _collect_names(item, names)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, ast.Node):
                                _collect_names(part, names)


class _Checker:
    def __init__(self, filename, lines):
        self.filename = filename
        self.lines = lines
        # Parallel stacks: scopes map name -> type, func_scopes map
        # name -> (param types, return type) for known user functions.
        self.scopes = [{}]
        self.func_scopes = [{}]
        # Ownership lite (3.0.0): parallel to scopes, maps an owned
        # (`let mut` array/dict) binding name -> True when live,
        # False once moved. Plain `let` and Copy values never appear
        # here. A name absent from every owned scope is not tracked.
        self.owned = [{}]
        # Stack of enclosing functions' return annotations (None when
        # the function has no return annotation, or when not in one).
        self.return_stack = []
        # 4.0.0 borrow checker state.
        # mut_bindings: parallel to scopes, the names in each scope
        # declared with `let mut` (params are never in here).
        self.mut_bindings = [set()]
        # binding_kinds: parallel to scopes, the origin of each
        # binding in its scope -- "let", "param", "for", or "match".
        # 6.0.0 immutable-by-default needs this so an assignment to
        # a non-`mut` binding can say WHAT it is, not just that it
        # is not mutable.
        self.binding_kinds = [{}]
        # borrow_frames: one {"id", "borrows"} list per function
        # being checked (the first is the top level). A borrow never
        # leaves its function: entering a function pushes a fresh
        # frame, leaving pops and discards it.
        self.borrow_frames = [{"id": 0, "borrows": []}]
        self._bframe_seq = 1
        # stmt_frames: one {"id", "last_use", "index"} per statement
        # list being checked (program, function body, block). A
        # borrow is owned by the statement list it was created in
        # and expires by that list's last-use map.
        self.stmt_frames = []
        self._frame_seq = 1
        # _pending: borrow records created and not yet adopted by a
        # `let` / assignment (temporaries stay here; adoption takes
        # only the records created during that one value check, by
        # sequence number, so leftovers can never be mis-adopted).
        self._pending = []
        self._borrow_seq = 0
        # func_stack: one {"params", "borrow_params"} per function
        # being checked, for the return-a-borrow rules.
        self.func_stack = []

    # -- plumbing ------------------------------------------------------

    def _fail(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        raise error_at(message, self.filename, self.lines, line, col)

    def _push_scope(self):
        self.scopes.append({})
        self.func_scopes.append({})
        self.owned.append({})
        self.mut_bindings.append(set())
        self.binding_kinds.append({})

    def _pop_scope(self):
        self.scopes.pop()
        self.func_scopes.pop()
        self.owned.pop()
        self.mut_bindings.pop()
        self.binding_kinds.pop()

    def _define(self, name, type_, kind="let"):
        self.scopes[-1][name] = type_
        self.binding_kinds[-1][name] = kind

    def _binding_kind(self, name):
        """The origin ("let" / "param" / "for" / "match") of the
        innermost binding of *name*, or None if it is not a known
        binding at all (the runtime owns it)."""
        for scope, kinds in zip(reversed(self.scopes),
                                reversed(self.binding_kinds)):
            if name in scope:
                return kinds.get(name, "let")
        return None

    def _lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        if name in _BUILTIN_RETURNS or name in _STDLIB_RETURNS:
            return "function"
        if name in ("pi", "e"):
            return "number"
        return "any"

    def _lookup_func(self, name):
        for scope in reversed(self.func_scopes):
            if name in scope:
                return scope[name]
        return None

    def _record_func(self, name, param_types, return_type):
        self.func_scopes[-1][name] = (param_types, return_type)

    # -- ownership lite (3.0.0) ---------------------------------------

    def _owned_state(self, name):
        """True if *name* is a live owned binding, False if moved,
        None if it is not an owned binding at all."""
        for scope in reversed(self.owned):
            if name in scope:
                return scope[name]
        return None

    def _mark_owned(self, name, live):
        """Set the live/moved state of an owned binding, in the scope
        that declared it."""
        for scope in reversed(self.owned):
            if name in scope:
                scope[name] = live
                return

    def _unown(self, name):
        """Stop tracking *name* as owned (it now holds a Copy value)."""
        for scope in reversed(self.owned):
            if name in scope:
                del scope[name]
                return

    def _maybe_move(self, expr):
        """If *expr* is a bare variable naming a live owned binding,
        move it (mark it moved). A use of an already-moved binding
        has already errored while the expression was checked, so this
        only ever sees live bindings. Moving a borrowed place is an
        error (borrow rule 3) -- but only owned bindings move at
        all; a plain `let` binding is never moved, so it can never
        hit this error."""
        if isinstance(expr, ast.Var):
            if self._owned_state(expr.name) is True:
                if self._live_borrows(expr.name):
                    self._fail(
                        expr,
                        "Cannot move '%s' while it is borrowed -- a "
                        "borrow of '%s' is still in use; use the "
                        "borrow first, or end it, before moving '%s'"
                        % (expr.name, expr.name, expr.name),
                    )
                self._mark_owned(expr.name, False)

    # -- borrow checker (4.0.0) ---------------------------------------

    @staticmethod
    def _types_compatible(expected, got):
        """True when a value of type *got* may be used where
        *expected* is wanted. Besides equality, ``&mut T`` coerces
        to ``&T``; a borrow never coerces to the plain type."""
        if expected == got:
            return True
        if (expected.startswith("&")
                and not expected.startswith("&mut ")
                and got.startswith("&mut ")):
            return expected[1:] == got[5:]
        return False

    def _is_mut_binding(self, name):
        """True if the innermost binding of *name* was declared with
        `let mut`, False if it was declared without it, None if the
        name is not a known binding at all (the runtime owns it)."""
        for scope, muts in zip(reversed(self.scopes),
                               reversed(self.mut_bindings)):
            if name in scope:
                return name in muts
        return None

    def _defined_at_top_level(self, name):
        return name in self.scopes[0]

    @staticmethod
    def _place_root(expr):
        """The variable a borrow or assignment touches, or None.

        ``x`` -> ``x``; ``x[i]`` / ``x[i][j]`` -> ``x`` (a borrow
        of ``a[i]`` borrows the whole container). Anything else --
        a call, a literal, a member -- is not a place."""
        if isinstance(expr, ast.Var):
            return expr.name
        if isinstance(expr, ast.Index):
            return _Checker._place_root(expr.target)
        return None

    def _live_borrows(self, place):
        """Live borrows of *place* in the current function."""
        return [b for b in self.borrow_frames[-1]["borrows"]
                if b["place"] == place]

    def _register_borrow(self, node, place, kind):
        """Record a new borrow of *place* ("shared" or "mut").

        It starts as a temporary, live only for the statement that
        created it; a `let` or assignment adopts it afterwards (see
        _adopt_borrows), extending it to the holder's last use."""
        frame = self.stmt_frames[-1] if self.stmt_frames else None
        record = {
            "place": place, "kind": kind, "holders": set(),
            "last_use": frame["index"] if frame is not None else 0,
            "owner": frame["id"] if frame is not None else 0,
            "bframe": self.borrow_frames[-1]["id"],
            "seq": self._borrow_seq, "node": node,
        }
        self._borrow_seq += 1
        self.borrow_frames[-1]["borrows"].append(record)
        self._pending.append(record)
        return record

    def _adopt_borrows(self, mark, holder):
        """Adopt borrows created since sequence number *mark* (i.e.
        while checking one `let` / assignment value) as held by
        *holder*. Their liveness becomes the holder's last use in
        the current statement list; an unmentioned holder means the
        borrow ends at its own statement."""
        frame = self.stmt_frames[-1] if self.stmt_frames else None
        current_bframe = self.borrow_frames[-1]["id"]
        rest = []
        for record in self._pending:
            if (record["seq"] >= mark
                    and record["bframe"] == current_bframe):
                record["holders"].add(holder)
                if frame is not None:
                    record["owner"] = frame["id"]
                    record["last_use"] = frame["last_use"].get(
                        holder, frame["index"])
            else:
                rest.append(record)
        self._pending = rest

    def _extend_borrow_holders(self, expr, holder):
        """A borrow copied into a new binding stays alive as long as
        the new binding is used: `let y = r` (or a match arm that
        yields `r`) makes `y` a holder too, so the borrow's last use
        becomes the later of the two holders' last uses.

        Walks *expr* for variables that currently hold a borrow;
        does not descend into nested function bodies (their borrows
        belong to them). Returns the records extended, so a
        reassignment can keep exactly those alive.
        """
        frame = self.stmt_frames[-1] if self.stmt_frames else None
        if frame is None:
            return []
        last = frame["last_use"].get(holder, frame["index"])
        extended = []
        for name in _borrow_holder_names(expr):
            for record in self.borrow_frames[-1]["borrows"]:
                if name in record["holders"]:
                    record["holders"].add(holder)
                    if last > record["last_use"]:
                        record["last_use"] = last
                    extended.append(record)
        return extended

    def _drop_holder_borrows(self, holder, keep=()):
        """End every borrow held by *holder* (it was reassigned),
        except the ones in *keep* (the new value still references
        them, e.g. `r = [r]`).

        A borrow with several holders loses only this one; it dies
        when its last holder is reassigned. A temporary borrow (no
        holders yet) is untouched -- it lives out its statement."""
        keep_ids = {id(record) for record in keep}
        live = self.borrow_frames[-1]["borrows"]
        kept = []
        for record in live:
            if (record["holders"] and holder in record["holders"]
                    and id(record) not in keep_ids):
                record["holders"].discard(holder)
                if record["holders"]:
                    kept.append(record)
            else:
                kept.append(record)
        live[:] = kept

    def _expire_borrows(self, frame):
        """Drop borrows owned by *frame* whose last use is past."""
        live = self.borrow_frames[-1]["borrows"]
        live[:] = [b for b in live
                   if not (b["owner"] == frame["id"]
                           and b["last_use"] < frame["index"])]

    def _drop_frame_borrows(self, frame_id):
        """Drop every borrow created in statement list *frame_id*
        (the block ended, so its holders are out of scope)."""
        live = self.borrow_frames[-1]["borrows"]
        live[:] = [b for b in live if b["owner"] != frame_id]

    def _check_statements(self, statements):
        """Check a statement list with NLL-lite liveness: borrows
        expire by last use as the statements are walked in order."""
        frame = {"id": self._frame_seq,
                 "last_use": _last_uses(statements), "index": 0}
        self._frame_seq += 1
        self.stmt_frames.append(frame)
        try:
            for index, stmt in enumerate(statements):
                frame["index"] = index
                self._expire_borrows(frame)
                self._check_stmt(stmt)
        finally:
            self.stmt_frames.pop()
            self._drop_frame_borrows(frame["id"])

    def _check_borrow(self, node):
        """Check a `&x` / `&mut x` expression; return its type."""
        if isinstance(node.expr, ast.BorrowExpr):
            self._fail(
                node,
                "Cannot borrow a borrow -- borrow the variable "
                "itself instead; reborrowing is not supported",
            )
        place = self._place_root(node.expr)
        if place is None:
            self._fail(
                node,
                "Can only borrow a variable or an element of an "
                "array or dict -- this is not something that can "
                "be borrowed",
            )
        if self._lookup(place).startswith("&"):
            self._fail(
                node,
                "Cannot borrow '%s' -- it already holds a borrow; "
                "borrow the original variable instead; reborrowing "
                "is not supported" % place,
            )
        # Borrowing a moved value is a use after move.
        self._check_use(node, place)
        if node.mutable:
            mut_state = self._is_mut_binding(place)
            if mut_state is False:
                self._fail(
                    node,
                    "Cannot borrow '%s' as mutable -- '%s' was not "
                    "declared with 'let mut'; only 'let mut' "
                    "bindings can be mutably borrowed" % (place, place),
                )
            if self._live_borrows(place):
                self._fail(
                    node,
                    "Cannot borrow '%s' as mutable -- it is already "
                    "borrowed; all borrows of '%s' must end first"
                    % (place, place),
                )
        else:
            for record in self._live_borrows(place):
                if record["kind"] == "mut":
                    self._fail(
                        node,
                        "Cannot borrow '%s' -- it is already "
                        "mutably borrowed; the mutable borrow must "
                        "end first" % place,
                    )
        value_type = self._check_expr(node.expr)
        if value_type == "any":
            # A borrow of an unknown value is itself unknown.
            borrow_type = "any"
        elif node.mutable:
            borrow_type = "&mut " + value_type
        else:
            borrow_type = "&" + value_type
        self._register_borrow(node, place,
                              "mut" if node.mutable else "shared")
        return borrow_type

    def _check_returnable_borrow(self, record, node):
        """Error if a borrow of a local is returned from a function.

        Only a borrow of a borrowed parameter (the lifetime-elision
        case) or of a top-level binding may be returned; anything
        else would not live long enough."""
        if not self.func_stack:
            return
        place = record["place"]
        if place in self.func_stack[-1]["borrow_params"]:
            return
        if self._defined_at_top_level(place):
            return
        self._fail(
            node,
            "Cannot return a borrow of '%s' -- '%s' is a local and "
            "will not live long enough; return a borrow of a "
            "borrowed parameter instead" % (place, place),
        )

    def _check_use(self, node, name):
        """Error if *name* is an owned binding that has been moved."""
        if self._owned_state(name) is False:
            self._fail(
                node,
                "Variable '%s' was used after move -- it was moved "
                "when it was assigned to another binding, passed to "
                "a function, or returned; assign a new value to it "
                "before using it again" % name,
            )

    def _validate_ann(self, ann):
        """Return the type string for a TypeAnn, or error if unknown.

        Borrow annotations keep their prefix: `&number` -> "&number",
        `&mut array` -> "&mut array"."""
        if ann is None:
            return None
        if ann.name not in VALID_TYPES:
            self._fail(ann, UNKNOWN_TYPE_MSG % ann.name)
        return ann.full_name

    def _ann_types(self, anns):
        """Validate a list of TypeAnn-or-None, keeping None entries."""
        return [self._validate_ann(a) for a in (anns or [])]

    # -- entry point ---------------------------------------------------

    def check_program(self, program):
        # Pre-pass: record top-level named functions so forward calls
        # and mutual recursion can be checked. Only signatures are
        # recorded -- annotations are validated in source order while
        # checking, so the FIRST error is still the earliest one.
        for stmt in program.statements:
            if isinstance(stmt, ast.FuncDecl) and stmt.name is not None:
                # Invalid names are left unrecorded here: the
                # statement check below raises the unknown-type error
                # in source order, and a call before the definition
                # must not report the bogus name first.
                param_types = [
                    a.full_name
                    if (a is not None and a.name in VALID_TYPES)
                    else None
                    for a in (stmt.param_types or [])
                ]
                return_type = None
                if (stmt.return_type is not None
                        and stmt.return_type.name in VALID_TYPES):
                    return_type = stmt.return_type.full_name
                self._record_func(stmt.name, param_types, return_type)
        self._check_statements(program.statements)

    # -- statements ----------------------------------------------------

    def _check_block(self, block):
        self._push_scope()
        try:
            self._check_statements(block.statements)
        finally:
            self._pop_scope()

    def _check_stmt(self, node):
        if isinstance(node, ast.LetStmt):
            self._check_let(node)
        elif isinstance(node, ast.AssignStmt):
            self._check_assign(node)
        elif isinstance(node, ast.ExprStmt):
            self._check_expr(node.expr)
        elif isinstance(node, ast.FuncDecl):
            self._check_func_decl(node, statement=True)
        elif isinstance(node, ast.ReturnStmt):
            self._check_return(node)
        elif isinstance(node, ast.IfStmt):
            for cond, body in node.branches:
                self._check_condition(cond)
                self._check_block(body)
            if node.else_body is not None:
                self._check_block(node.else_body)
        elif isinstance(node, ast.WhileStmt):
            self._check_condition(node.condition)
            self._check_block(node.body)
        elif isinstance(node, ast.ForStmt):
            self._check_for(node)
        elif isinstance(node, (ast.BreakStmt, ast.ContinueStmt)):
            pass
        elif isinstance(node, ast.ImportStmt):
            pass  # imports bring in unknown names; nothing to check
        else:  # pragma: no cover - parser only builds known nodes
            pass

    def _check_condition(self, cond):
        type_ = self._check_expr(cond)
        if type_ != "any" and type_ != "bool":
            self._fail(
                cond,
                "This condition is a %s, but 'if' and 'while' need a "
                "bool -- try a comparison like == or <" % type_,
            )

    def _check_let(self, node):
        ann = self._validate_ann(node.type_ann)
        mark = self._borrow_seq
        value_type = self._check_expr(node.value)
        # Borrows created by the value are held by this binding.
        self._adopt_borrows(mark, node.name)
        # A borrow copied into this binding (let y = r, or a match
        # arm that yields r) keeps the new binding as a holder too.
        self._extend_borrow_holders(node.value, node.name)
        # A bare owned variable as the whole value moves it into the
        # new binding (whatever kind of `let` the target is).
        self._maybe_move(node.value)
        if isinstance(node.value, ast.FuncDecl):
            # A let-bound anonymous function is a known user function
            # for later call-site checks.
            param_types = self._ann_types(node.value.param_types)
            return_type = self._validate_ann(node.value.return_type)
            self._record_func(node.name, param_types, return_type)
        if ann is not None:
            if (ann != "any" and value_type != "any"
                    and not self._types_compatible(ann, value_type)):
                self._fail(
                    node.value,
                    "Variable '%s' is declared as %s, but the value "
                    "is a %s -- give it a %s, or change the annotation"
                    % (node.name, ann, value_type, ann),
                )
            self._define(node.name, ann)
        else:
            self._define(node.name, value_type)
        if getattr(node, "mutable", False):
            self.mut_bindings[-1].add(node.name)
        # Ownership lite: only `let mut` over a statically known
        # array/dict is owned. `let mut` on a Copy value is a no-op
        # marker; an unknown ("any") value is never tracked.
        if getattr(node, "mutable", False):
            effective = ann if ann is not None else value_type
            if effective in ("array", "dict"):
                self.owned[-1][node.name] = True

    def _immutable_msg(self, name):
        """The 6.0.0 error for assigning to a non-`mut` binding,
        naming what the binding is so the fix is obvious."""
        kind = self._binding_kind(name)
        if kind == "param":
            return ("Cannot assign to '%s' -- function parameters are "
                    "immutable; use a local 'let mut' binding instead"
                    % name)
        if kind == "for":
            return ("Cannot assign to '%s' -- a 'for' loop variable "
                    "is immutable; use a local 'let mut' binding "
                    "instead" % name)
        if kind == "match":
            return ("Cannot assign to '%s' -- a 'match' binding is "
                    "immutable; use a local 'let mut' binding instead"
                    % name)
        if kind == "func":
            return ("Cannot assign to '%s' -- a function cannot be "
                    "reassigned; use a 'let mut' binding instead"
                    % name)
        return ("Cannot assign to '%s' -- '%s' was declared with "
                "plain 'let', which is immutable; declare it with "
                "'let mut %s = ...' to make it mutable"
                % (name, name, name))

    def _check_assign(self, node):
        # 6.0.0 immutable by default: a plain `let` binding (and a
        # function parameter, `for` variable, `match` binding, or
        # function name -- none of which can be `let mut`) cannot be
        # reassigned. A name the checker does not know is the
        # runtime's business, not ours, so it is left alone.
        if (isinstance(node.target, ast.Var)
                and self._is_mut_binding(node.target.name) is False):
            self._fail(node.target,
                       self._immutable_msg(node.target.name))
        # A borrowed place cannot be written while the borrow is
        # live: no reassignment, no compound assignment, no element
        # write through it.
        place = None
        if isinstance(node.target, (ast.Var, ast.Index)):
            place = self._place_root(node.target)
        if place is not None and self._live_borrows(place):
            self._fail(
                node.target,
                "Cannot assign to '%s' while it is borrowed -- a "
                "borrow of '%s' is still in use; end the borrow "
                "first" % (place, place),
            )
        # Reassignment ends any borrow the target itself held --
        # unless the new value still references it (e.g. `r = [r]`).
        # That is decided after the value is checked, below.
        # Reassignment re-initializes a moved-from binding, so a
        # plain `=` to a moved owned variable is not a use-after-move
        # error. Compound assignment (+=, -=) reads the old value,
        # so it still is.
        if (node.op == "=" and isinstance(node.target, ast.Var)
                and self._owned_state(node.target.name) is False):
            target_type = self._lookup(node.target.name)
        else:
            target_type = self._check_expr(node.target)
        mark = self._borrow_seq
        value_type = self._check_expr(node.value)
        # A borrow stored by this assignment is held by the target.
        kept = []
        if isinstance(node.target, ast.Var):
            self._adopt_borrows(mark, node.target.name)
            kept = self._extend_borrow_holders(node.value,
                                               node.target.name)
        elif place is not None:
            self._adopt_borrows(mark, place)
            kept = self._extend_borrow_holders(node.value, place)
        if isinstance(node.target, ast.Var):
            self._drop_holder_borrows(node.target.name, keep=kept)
        elif isinstance(node.target, ast.Index) and place is not None:
            self._drop_holder_borrows(place, keep=kept)
        # A bare owned variable as the whole value moves it.
        self._maybe_move(node.value)
        if node.op == "+=":
            self._check_plus(node, target_type, value_type)
        elif node.op == "-=":
            self._check_number_operands(node, "-=", target_type, value_type)
        # Reassignment to an owned binding re-initializes it: live
        # again with an array/dict, un-owned with a Copy value, and
        # conservatively still owned-and-live when the new type is
        # unknown ("any").
        if node.op == "=" and isinstance(node.target, ast.Var):
            name = node.target.name
            if self._owned_state(name) is not None:
                if value_type in ("array", "dict", "any", "function"):
                    self._mark_owned(name, True)
                else:  # number, string, bool, null: Copy now
                    self._unown(name)

    def _check_return(self, node):
        if node.value is None:
            value_type = "null"
        else:
            mark = self._borrow_seq
            value_type = self._check_expr(node.value)
            # Returning a bare owned variable moves it out.
            self._maybe_move(node.value)
            # A borrow created by the returned value must not be a
            # borrow of a local (it would not live long enough).
            current_bframe = self.borrow_frames[-1]["id"]
            for record in list(self._pending):
                if (record["seq"] >= mark
                        and record["bframe"] == current_bframe):
                    self._check_returnable_borrow(record, node.value)
            # Returning a variable that holds a borrow of a local.
            if isinstance(node.value, ast.Var):
                for record in self.borrow_frames[-1]["borrows"]:
                    if node.value.name in record["holders"]:
                        self._check_returnable_borrow(record, node.value)
        if not self.return_stack:
            return  # 'return' outside a function: the runtime owns it
        expected = self.return_stack[-1]
        if expected is None or expected == "any":
            return
        if value_type != "any" and not self._types_compatible(
                expected, value_type):
            self._fail(
                node.value if node.value is not None else node,
                "This function should return a %s, but this 'return' "
                "gives a %s -- return a %s instead"
                % (expected, value_type, expected),
            )

    def _check_for(self, node):
        iter_type = self._check_expr(node.iterable)
        if iter_type != "any" and iter_type not in ("array", "string", "dict"):
            self._fail(
                node.iterable,
                "'for' needs an array, a string, or a dict to loop "
                "over, but this is a %s" % iter_type,
            )
        if iter_type == "string" or iter_type == "dict":
            elem_type = "string"  # chars for strings, keys for dicts
        else:
            elem_type = "any"
        self._push_scope()
        try:
            self._define(node.var, elem_type, kind="for")
            self._check_block(node.body)
        finally:
            self._pop_scope()

    def _check_func_decl(self, node, statement):
        param_types = self._ann_types(node.param_types)
        return_type = self._validate_ann(node.return_type)
        # Lifetime elision: a function returning a borrow must take
        # exactly one borrowed parameter, else the returned borrow
        # could not be tied to any input.
        if return_type is not None and return_type.startswith("&"):
            borrowed = [t for t in param_types
                        if t is not None and t.startswith("&")]
            if len(borrowed) != 1:
                name = node.name or "<anonymous>"
                if not borrowed:
                    detail = "it takes no borrowed parameters"
                else:
                    detail = ("it takes %d borrowed parameters"
                              % len(borrowed))
                self._fail(
                    node,
                    "Function '%s' returns a borrow, but lifetime "
                    "elision needs exactly one borrowed parameter "
                    "-- %s" % (name, detail),
                )
        if statement and node.name is not None:
            # Register before checking the body so recursion works.
            self._define(node.name, "function", kind="func")
            self._record_func(node.name, param_types, return_type)
        self._push_scope()
        # A borrow never leaves its function: fresh borrow frame,
        # discarded when the function has been checked.
        bframe = {"id": self._bframe_seq, "borrows": []}
        self._bframe_seq += 1
        self.borrow_frames.append(bframe)
        self.func_stack.append({
            "params": set(node.params),
            "borrow_params": {
                name for name, type_ in zip(node.params, param_types)
                if type_ is not None and type_.startswith("&")},
        })
        try:
            for name, type_ in zip(node.params, param_types):
                self._define(name, type_ if type_ is not None else "any",
                             kind="param")
            # Any params beyond the annotation list (cannot happen
            # from the parser, but be safe) are untyped.
            for name in node.params[len(param_types):]:
                self._define(name, "any", kind="param")
            self.return_stack.append(return_type)
            try:
                self._check_statements(node.body.statements)
            finally:
                self.return_stack.pop()
        finally:
            self.func_stack.pop()
            self.borrow_frames.pop()
            self._pending = [b for b in self._pending
                             if b["bframe"] != bframe["id"]]
            self._pop_scope()

    # -- expressions ---------------------------------------------------

    def _check_expr(self, node):
        if isinstance(node, ast.Literal):
            return _literal_type(node.value)
        if isinstance(node, ast.StringLiteral):
            for kind, part in node.parts:
                if kind == "expr":
                    # Interpolation parts are source text, not AST;
                    # nothing the checker can inspect.
                    pass
            return "string"
        if isinstance(node, ast.Var):
            self._check_use(node, node.name)
            # Borrow rule 1: while a mutable borrow of a place is
            # live, no other access to that place -- not even a read.
            # (Shared borrows still allow reads.)
            for record in self.borrow_frames[-1]["borrows"]:
                if (record["place"] == node.name
                        and record["kind"] == "mut"):
                    self._fail(
                        node,
                        "Cannot use '%s' while it is mutably borrowed "
                        "-- the mutable borrow must end first"
                        % node.name,
                    )
            return self._lookup(node.name)
        if isinstance(node, ast.BorrowExpr):
            return self._check_borrow(node)
        if isinstance(node, ast.ArrayLiteral):
            for element in node.elements:
                self._check_expr(element)
            return "array"
        if isinstance(node, ast.DictLiteral):
            for _key, value in node.pairs:
                self._check_expr(value)
            return "dict"
        if isinstance(node, ast.BinaryOp):
            return self._check_binary(node)
        if isinstance(node, ast.UnaryOp):
            self._check_expr(node.operand)
            return "number" if node.op == "-" else "bool"
        if isinstance(node, ast.Call):
            return self._check_call(node)
        if isinstance(node, ast.Index):
            target_type = self._check_expr(node.target)
            self._check_expr(node.index)
            if target_type == "string":
                return "string"
            return "any"  # array / dict elements, and anything unknown
        if isinstance(node, ast.Member):
            self._check_expr(node.object)
            if node.name in _STRING_METHODS:
                return "function"
            return "any"
        if isinstance(node, ast.FuncDecl):
            self._check_func_decl(node, statement=False)
            return "function"
        if isinstance(node, ast.MatchExpr):
            return self._check_match(node)
        return "any"  # pragma: no cover - unknown node kind

    def _check_plus(self, node, left, right):
        if left != "any" and right != "any":
            if not (left == right and left in ("number", "string", "array")):
                self._fail(
                    node,
                    "Cannot add a %s and a %s -- '+' works on two "
                    "numbers, two strings, or two arrays" % (left, right),
                )

    def _check_number_operands(self, node, op, left, right):
        if left != "any" and left != "number":
            self._fail(
                node,
                "'%s' needs numbers, but the left side is a %s"
                % (op, left),
            )
        if right != "any" and right != "number":
            self._fail(
                node,
                "'%s' needs numbers, but the right side is a %s"
                % (op, right),
            )

    def _check_binary(self, node):
        left = self._check_expr(node.left)
        right = self._check_expr(node.right)
        op = node.op
        if op == "+":
            self._check_plus(node, left, right)
            if left == right and left in ("number", "string", "array"):
                return left
            return "any"
        if op in ("-", "*", "/", "%", "**"):
            self._check_number_operands(node, op, left, right)
            if left == "number" and right == "number":
                return "number"
            return "any"
        if op in ("<", "<=", ">", ">="):
            if left != "any" and right != "any":
                if not (left == right
                        and left in ("number", "string")):
                    self._fail(
                        node,
                        "Cannot compare a %s with a %s using '%s' -- "
                        "comparisons need two numbers or two strings"
                        % (left, right, op),
                    )
            return "bool"
        if op in ("==", "!="):
            return "bool"
        if op in ("and", "or"):
            return "any"
        return "any"  # pragma: no cover - unknown operator

    # -- calls ---------------------------------------------------------

    def _check_arg(self, node, arg_types, index, allowed, what):
        """Error if a known argument type is outside *allowed*."""
        if index >= len(arg_types):
            return
        got = arg_types[index]
        if got != "any" and got not in allowed:
            self._fail(
                node,
                "%s, but got a %s -- pass %s instead"
                % (what, got, " or ".join(sorted(allowed))),
            )

    def _check_builtin_call(self, name, node, arg_types):
        if name == "print":
            pass  # any arguments, any count
        elif name == "input":
            self._check_arg(node, arg_types, 0, {"string"},
                            "input() needs a string prompt")
        elif name == "len":
            self._check_arg(node, arg_types, 0,
                            {"string", "array", "dict"},
                            "len() works on a string, array, or dict")
        elif name == "str" or name == "type":
            pass  # any single argument
        elif name == "num":
            self._check_arg(node, arg_types, 0, {"string", "number"},
                            "num() works on a string or a number")
        elif name == "range":
            self._check_arg(node, arg_types, 0, {"number"},
                            "range() needs whole numbers")
            self._check_arg(node, arg_types, 1, {"number"},
                            "range() needs whole numbers")
        elif name == "push":
            self._check_arg(node, arg_types, 0, {"array"},
                            "push() needs an array first")
        elif name == "pop":
            self._check_arg(node, arg_types, 0, {"array"},
                            "pop() needs an array")
        elif name == "sqrt" or name == "abs":
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs a number" % name)
        elif name == "pow":
            self._check_arg(node, arg_types, 0, {"number"},
                            "pow() needs two numbers")
            self._check_arg(node, arg_types, 1, {"number"},
                            "pow() needs two numbers")
        elif name == "keys":
            self._check_arg(node, arg_types, 0, {"dict"},
                            "keys() needs a dict")
        elif name in ("int", "float"):
            self._check_arg(node, arg_types, 0, {"string", "number"},
                            "%s() works on a string or a number" % name)
        return _BUILTIN_RETURNS[name]

    def _check_stdlib_call(self, name, node, arg_types):
        if name in ("randint", "randrange"):
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs numbers" % name)
            self._check_arg(node, arg_types, 1, {"number"},
                            "%s() needs numbers" % name)
        elif name == "choice":
            self._check_arg(node, arg_types, 0, {"array"},
                            "choice() needs an array")
        elif name == "shuffle":
            self._check_arg(node, arg_types, 0, {"array"},
                            "shuffle() needs an array")
        elif name == "seed":
            self._check_arg(node, arg_types, 0, {"number"},
                            "seed() needs a number")
        elif name in ("read_file", "file_exists", "http_get"):
            self._check_arg(node, arg_types, 0, {"string"},
                            "%s() needs a string path or URL" % name)
        elif name == "write_file":
            self._check_arg(node, arg_types, 0, {"string"},
                            "write_file() needs a string path")
            self._check_arg(node, arg_types, 1, {"string"},
                            "write_file() needs string contents")
        elif name == "json_parse":
            self._check_arg(node, arg_types, 0, {"string"},
                            "json_parse() needs a JSON string")
        elif name == "json_stringify":
            pass  # any single value
        elif name == "http_post":
            self._check_arg(node, arg_types, 0, {"string"},
                            "http_post() needs a string URL")
            self._check_arg(node, arg_types, 1, {"string"},
                            "http_post() needs a string body")
        elif name == "now":
            pass  # no arguments
        elif name == "sleep":
            self._check_arg(node, arg_types, 0, {"number"},
                            "sleep() needs a number of seconds")
        else:  # floor, ceil, sin, cos, tan, log, exp
            self._check_arg(node, arg_types, 0, {"number"},
                            "%s() needs a number" % name)
        return _STDLIB_RETURNS[name]

    def _check_call(self, node):
        arg_types = [self._check_expr(arg) for arg in node.args]
        # push()/pop() mutate their array in place, so calling one
        # on a borrowed place is a mutation while borrowed.
        callee = node.callee
        if (isinstance(callee, ast.Var)
                and callee.name in ("push", "pop")
                and node.args
                and isinstance(node.args[0], ast.Var)
                and self._live_borrows(node.args[0].name)):
            place = node.args[0].name
            self._fail(
                node.args[0],
                "Cannot change '%s' while it is borrowed -- '%s' "
                "needs exclusive access to '%s'"
                % (place, callee.name, place),
            )
        # Passing a bare owned variable as an argument moves it --
        # except to a known read-only builtin / stdlib function
        # (6.0.0): those take a shared borrow of their arguments for
        # the call and never move. A user function that shadows a
        # builtin name still moves, like any user function.
        read_only = (
            isinstance(callee, ast.Var)
            and self._lookup_func(callee.name) is None
            and (callee.name in _READ_ONLY_BUILTINS
                 or callee.name in _READ_ONLY_STDLIB))
        if not read_only:
            for arg in node.args:
                self._maybe_move(arg)
        callee = node.callee
        if isinstance(callee, ast.Var):
            name = callee.name
            sig = self._lookup_func(name)
            if sig is not None:
                param_types, return_type = sig
                for index, expected in enumerate(param_types):
                    if expected is None or expected == "any":
                        continue
                    if index < len(arg_types):
                        got = arg_types[index]
                        if (got != "any"
                                and not self._types_compatible(
                                    expected, got)):
                            self._fail(
                                node.args[index],
                                "Argument %d to '%s' should be a %s, "
                                "but got a %s" % (
                                    index + 1, name, expected, got),
                            )
                return return_type if return_type is not None else "any"
            if name in _BUILTIN_RETURNS:
                return self._check_builtin_call(name, node, arg_types)
            if name in _STDLIB_RETURNS:
                return self._check_stdlib_call(name, node, arg_types)
            return "any"  # unknown callee: the runtime owns it
        if isinstance(callee, ast.Member):
            receiver_type = self._check_expr(callee.object)
            method = callee.name
            if method in _STRING_METHODS:
                if receiver_type != "any" and receiver_type != "string":
                    self._fail(
                        callee,
                        "Method '%s' works on a string, but this is "
                        "a %s" % (method, receiver_type),
                    )
                allowed, return_type = _STRING_METHODS[method]
                for index, allowed_set in enumerate(allowed):
                    self._check_arg(
                        node, arg_types, index, allowed_set,
                        "'%s()' needs %s" % (
                            method, " or ".join(sorted(allowed_set))))
                return return_type
            return "any"
        # Callee is some other expression (a call result, a match,
        # an anonymous func, ...): check it and give up on the type.
        self._check_expr(callee)
        return "any"

    # -- match ---------------------------------------------------------

    def _check_match(self, node):
        scrutinee_type = self._check_expr(node.scrutinee)
        body_types = []
        for arm in node.arms:
            self._push_scope()
            try:
                pattern = arm.pattern
                if isinstance(pattern, ast.Literal):
                    pattern_type = _literal_type(pattern.value)
                    if (scrutinee_type != "any"
                            and pattern_type != scrutinee_type):
                        self._fail(
                            pattern,
                            "This pattern is a %s, but 'match' is "
                            "matching a %s -- use a %s pattern, or '_'"
                            % (pattern_type, scrutinee_type,
                               scrutinee_type),
                        )
                elif isinstance(pattern, ast.Var):
                    # A binding arm binds the scrutinee's type.
                    self._define(pattern.name, scrutinee_type,
                                 kind="match")
                # Wildcard: binds nothing.
                if arm.guard is not None:
                    guard_type = self._check_expr(arm.guard)
                    if guard_type != "any" and guard_type != "bool":
                        self._fail(
                            arm.guard,
                            "A 'match' guard must be a bool, but this "
                            "is a %s" % guard_type,
                        )
                body_types.append(self._check_expr(arm.body))
            finally:
                self._pop_scope()
        if body_types and all(t == body_types[0] for t in body_types):
            return body_types[0]
        return "any"


def check_program(program, filename="<stdin>", lines=None):
    """Type-check *program* (a parsed AST).

    Raises ``KabirError`` with file/line on the FIRST type error;
    returns ``None`` on success. Untyped code always passes.
    """
    _Checker(filename, lines).check_program(program)
    return None
