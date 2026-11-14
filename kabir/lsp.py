"""Language server for Kabir (``kabir lsp``).

A small LSP server over stdio speaking JSON-RPC with Content-Length
framing, as specified by the Language Server Protocol. It is driven by
the same pieces as the rest of the language: ``parse_source`` for
syntax, ``check_program`` for type diagnostics, ``format_source`` for
formatting.

Methods implemented: ``initialize``, ``initialized``,
``textDocument/didOpen``, ``textDocument/didChange``,
``textDocument/didClose``, ``textDocument/completion``,
``textDocument/hover``, ``textDocument/definition``,
``textDocument/formatting``, ``shutdown``, ``exit``.

Unknown methods get a MethodNotFound (-32601) response, never a crash.
Diagnostics are computed on open/change from parse + type-check errors
(the checker reports its first error only, so at most one diagnostic
per file at a time from the checker; a parse error likewise yields one).

The server is transport-independent: ``LanguageServer.handle_message``
takes one decoded JSON-RPC message dict and returns a list of outgoing
message dicts, so tests drive it in-process with no editor and no
subprocess.
"""

import json
import re
import sys

from . import __version__, ast_nodes as ast
from .checker import (
    _BUILTIN_RETURNS,
    _STDLIB_RETURNS,
    _STRING_METHODS,
    check_program,
)
from .errors import KabirError
from .formatter import format_source
from .lexer import KEYWORDS
from .parser import parse as parse_source

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
PARSE_ERROR = -32700

_TYPE_NAMES = ("number", "string", "bool", "array", "dict", "function",
               "any", "null")


# ----------------------------------------------------------------------
# Framing: Content-Length headers, exactly as LSP specifies.
# ----------------------------------------------------------------------

def encode_message(message):
    """Encode one JSON-RPC message dict to framed bytes."""
    body = json.dumps(message).encode("utf-8")
    header = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
    return header + body


def read_message(stream):
    """Read one framed message from a binary stream.

    Returns the decoded dict, or None on EOF. Raises ValueError on a
    malformed header or body.
    """
    headers = {}
    while True:
        line = stream.readline()
        if line in (b"", None):
            return None
        line = line.strip()
        if not line:
            break
        if b":" not in line:
            raise ValueError("malformed LSP header: %r" % line)
        name, value = line.split(b":", 1)
        headers[name.strip().lower().decode("ascii")] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(stream, message):
    """Write one framed message to a binary stream."""
    stream.write(encode_message(message))
    stream.flush()


# ----------------------------------------------------------------------
# Analysis: definitions, references, and inferred types for one file.
# ----------------------------------------------------------------------

def _literal_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "any"


def _word_at(text, line, character):
    """The identifier under a 0-based (line, character) position.

    Returns (name, start_char, end_char) or None.
    """
    lines = text.split("\n")
    if line < 0 or line >= len(lines):
        return None
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", lines[line]):
        if match.start() <= character <= match.end():
            return match.group(0), match.start(), match.end()
    return None


class _Analyzer:
    """Walk a parsed program, collecting definitions and references.

    Scopes are tuples of ints (``()`` is the top level); a definition
    is visible from a reference when its scope is a prefix of the
    reference's scope. Types are inferred with the same rules as the
    checker where the checker has an opinion (literals, annotations,
    builtin/stdlib return types); anything unknown is ``"any"``.
    """

    def __init__(self, source, filename):
        self.source = source
        self.lines = source.split("\n")
        self.program = parse_source(source, filename)
        self.defs = []   # {name, line, col, kind, type, detail, scope}
        self.refs = []   # {name, line, col, scope}
        self._func_returns = {}  # name -> annotated return type or None
        self._scope_counter = 0
        self._walk_stmts(self.program.statements, (), [{}])

    # -- scopes --------------------------------------------------------
    def _child_scope(self, scope):
        self._scope_counter += 1
        return scope + (self._scope_counter,)

    @staticmethod
    def _is_prefix(scope, other):
        return len(scope) <= len(other) and other[:len(scope)] == scope

    # -- definitions ---------------------------------------------------
    def _name_col(self, node, name, search_from_col=None):
        """1-based column of *name* on the node's line.

        The AST records the statement's starting column, not the
        name's, so the name is found by searching the source line
        from just past the leading keyword.
        """
        if node.line is not None and 1 <= node.line <= len(self.lines):
            text = self.lines[node.line - 1]
            start = max(0, (search_from_col or node.col or 1) - 1)
            match = re.search(r"\b" + re.escape(name) + r"\b", text[start:])
            if match:
                return start + match.start() + 1
        return node.col or 1

    def _add_def(self, name, node, scope, kind, type_, detail,
                 search_from_col=None):
        self.defs.append({
            "name": name,
            "line": node.line or 1,
            "col": self._name_col(node, name, search_from_col),
            "kind": kind,
            "type": type_,
            "detail": detail,
            "scope": scope,
        })

    def _signature(self, node):
        types = getattr(node, "param_types", None) or [None] * len(node.params)
        params = ", ".join(
            p if t is None else "%s: %s" % (p, t.full_name)
            for p, t in zip(node.params, types))
        ret = getattr(node, "return_type", None)
        ret_text = (" -> %s" % ret.full_name) if ret is not None else ""
        # 7.0.0 lifetimes: `func f<'a, 'b>(...)`.
        lifetimes = getattr(node, "lifetimes", None) or []
        lt_text = ("<'%s>" % ", '".join(lifetimes)) if lifetimes else ""
        if node.name is not None:
            return "func %s%s(%s)%s" % (node.name, lt_text, params,
                                        ret_text)
        return "func%s(%s)%s" % (lt_text, params, ret_text)

    # -- type inference ------------------------------------------------
    def _lookup_type(self, env, name):
        for scope_env in reversed(env):
            if name in scope_env:
                return scope_env[name]
        if name in _BUILTIN_RETURNS or name in _STDLIB_RETURNS:
            return "function"
        if name in ("pi", "e"):
            return "number"
        return "any"

    def _infer(self, node, env):
        if isinstance(node, ast.Literal):
            return _literal_type(node.value)
        if isinstance(node, ast.StringLiteral):
            return "string"
        if isinstance(node, ast.Var):
            return self._lookup_type(env, node.name)
        if isinstance(node, ast.ArrayLiteral):
            return "array"
        if isinstance(node, ast.DictLiteral):
            return "dict"
        if isinstance(node, ast.BinaryOp):
            if node.op in ("==", "!=", "<", "<=", ">", ">=", "and", "or"):
                return "bool"
            if node.op == "+":
                left = self._infer(node.left, env)
                right = self._infer(node.right, env)
                if left == right and left in ("number", "string", "array"):
                    return left
                return "any"
            return "number"
        if isinstance(node, ast.UnaryOp):
            return "number" if node.op == "-" else "bool"
        if isinstance(node, ast.BorrowExpr):
            inner = self._infer(node.expr, env)
            if inner == "any":
                return "any"
            return "&mut " + inner if node.mutable else "&" + inner
        if isinstance(node, ast.DerefExpr):
            # 7.0.0: `*r` has the plain type the borrow borrows --
            # strip `&`, an optional `'a` lifetime, and `mut `.
            inner = self._infer(node.operand, env)
            if inner == "any" or not inner.startswith("&"):
                return inner if inner != "any" else "any"
            rest = inner[1:]
            if rest.startswith("'"):
                space = rest.find(" ")
                if space == -1:
                    return "any"
                rest = rest[space + 1:]
            if rest.startswith("mut "):
                rest = rest[4:]
            return rest
        if isinstance(node, ast.Call):
            callee = node.callee
            if isinstance(callee, ast.Var):
                if callee.name in _BUILTIN_RETURNS:
                    return _BUILTIN_RETURNS[callee.name]
                if callee.name in _STDLIB_RETURNS:
                    return _STDLIB_RETURNS[callee.name]
                if callee.name in self._func_returns:
                    ret = self._func_returns[callee.name]
                    return ret if ret is not None else "any"
            if isinstance(callee, ast.Member):
                if callee.name in _STRING_METHODS:
                    return _STRING_METHODS[callee.name][1]
            return "any"
        if isinstance(node, ast.Index):
            target = self._infer(node.target, env)
            return "string" if target == "string" else "any"
        if isinstance(node, ast.Member):
            return "function" if node.name in _STRING_METHODS else "any"
        if isinstance(node, ast.FuncDecl):
            return "function"
        if isinstance(node, ast.MatchExpr):
            if node.arms:
                return self._infer(node.arms[0].body, env)
            return "any"
        return "any"

    # -- walking -------------------------------------------------------
    def _walk_stmts(self, stmts, scope, env):
        for stmt in stmts:
            self._walk_stmt(stmt, scope, env)

    def _walk_block(self, block, scope, env):
        child = self._child_scope(scope)
        env.append({})
        try:
            self._walk_stmts(block.statements, child, env)
        finally:
            env.pop()

    def _walk_func(self, node, scope, env, name):
        if name is not None:
            self._add_def(name, node, scope, "function", "function",
                          self._signature(node),
                          search_from_col=(node.col or 1) + 4)
            env[-1][name] = "function"
            ret = getattr(node, "return_type", None)
            self._func_returns[name] = ret.full_name if ret is not None else None
        child = self._child_scope(scope)
        env.append({})
        try:
            types = getattr(node, "param_types", None) or [None] * len(node.params)
            for pname, pann in zip(node.params, types):
                ptype = pann.full_name if pann is not None else "any"
                self._add_def(pname, node, child, "parameter", ptype,
                              "param %s: %s" % (pname, ptype),
                              search_from_col=(node.col or 1) + 4)
                env[-1][pname] = ptype
            self._walk_stmts(node.body.statements, child, env)
        finally:
            env.pop()

    def _walk_stmt(self, stmt, scope, env):
        if isinstance(stmt, ast.LetStmt):
            self._walk_expr(stmt.value, scope, env)
            type_ = self._infer(stmt.value, env)
            ann = stmt.type_ann.full_name if stmt.type_ann is not None else None
            if ann is not None:
                type_ = ann
            self._add_def(stmt.name, stmt, scope, "variable", type_,
                          "let %s: %s" % (stmt.name, type_),
                          search_from_col=(stmt.col or 1) + 3)
            env[-1][stmt.name] = type_
        elif isinstance(stmt, ast.FuncDecl):
            self._walk_func(stmt, scope, env, stmt.name)
        elif isinstance(stmt, ast.AssignStmt):
            self._walk_expr(stmt.target, scope, env)
            self._walk_expr(stmt.value, scope, env)
        elif isinstance(stmt, ast.ExprStmt):
            self._walk_expr(stmt.expr, scope, env)
        elif isinstance(stmt, ast.ReturnStmt):
            if stmt.value is not None:
                self._walk_expr(stmt.value, scope, env)
        elif isinstance(stmt, ast.IfStmt):
            for cond, body in stmt.branches:
                self._walk_expr(cond, scope, env)
                self._walk_block(body, scope, env)
            if stmt.else_body is not None:
                self._walk_block(stmt.else_body, scope, env)
        elif isinstance(stmt, ast.WhileStmt):
            self._walk_expr(stmt.condition, scope, env)
            self._walk_block(stmt.body, scope, env)
        elif isinstance(stmt, ast.ForStmt):
            self._walk_expr(stmt.iterable, scope, env)
            iter_type = self._infer(stmt.iterable, env)
            elem = "string" if iter_type in ("string", "dict") else "any"
            child = self._child_scope(scope)
            env.append({})
            try:
                self._add_def(stmt.var, stmt, child, "variable", elem,
                              "for %s: %s" % (stmt.var, elem),
                              search_from_col=(stmt.col or 1) + 3)
                env[-1][stmt.var] = elem
                self._walk_block(stmt.body, child, env)
            finally:
                env.pop()
        elif isinstance(stmt, (ast.BreakStmt, ast.ContinueStmt,
                               ast.ImportStmt)):
            pass

    def _walk_expr(self, node, scope, env):
        if isinstance(node, ast.Var):
            self.refs.append({"name": node.name, "line": node.line or 1,
                              "col": node.col or 1, "scope": scope})
        elif isinstance(node, ast.ArrayLiteral):
            for element in node.elements:
                self._walk_expr(element, scope, env)
        elif isinstance(node, ast.DictLiteral):
            for _key, value in node.pairs:
                self._walk_expr(value, scope, env)
        elif isinstance(node, ast.BinaryOp):
            self._walk_expr(node.left, scope, env)
            self._walk_expr(node.right, scope, env)
        elif isinstance(node, ast.UnaryOp):
            self._walk_expr(node.operand, scope, env)
        elif isinstance(node, ast.BorrowExpr):
            self._walk_expr(node.expr, scope, env)
        elif isinstance(node, ast.DerefExpr):
            # 7.0.0: walk the borrowed value, so `*r` counts as a
            # use of `r` for references and rename.
            self._walk_expr(node.operand, scope, env)
        elif isinstance(node, ast.Call):
            self._walk_expr(node.callee, scope, env)
            for arg in node.args:
                self._walk_expr(arg, scope, env)
        elif isinstance(node, ast.Index):
            self._walk_expr(node.target, scope, env)
            self._walk_expr(node.index, scope, env)
        elif isinstance(node, ast.Member):
            self._walk_expr(node.object, scope, env)
        elif isinstance(node, ast.FuncDecl):
            self._walk_func(node, scope, env, node.name)
        elif isinstance(node, ast.MatchExpr):
            self._walk_expr(node.scrutinee, scope, env)
            scrut_type = self._infer(node.scrutinee, env)
            for arm in node.arms:
                child = self._child_scope(scope)
                env.append({})
                try:
                    if isinstance(arm.pattern, ast.Var):
                        self._add_def(arm.pattern.name, arm, child,
                                      "variable", scrut_type,
                                      "match %s: %s"
                                      % (arm.pattern.name, scrut_type))
                        env[-1][arm.pattern.name] = scrut_type
                    if arm.guard is not None:
                        self._walk_expr(arm.guard, child, env)
                    self._walk_expr(arm.body, child, env)
                finally:
                    env.pop()

    # -- lookup --------------------------------------------------------
    def resolve(self, name, scope, line):
        """The definition visible from (scope, line), or None.

        Prefers the deepest enclosing scope, then the latest
        definition at or before the use; if nothing precedes the use
        (a function called before its declaration), falls back to the
        earliest visible definition.
        """
        visible = [d for d in self.defs
                   if d["name"] == name
                   and self._is_prefix(d["scope"], scope)]
        if not visible:
            return None
        before = [d for d in visible if d["line"] <= line]
        pool = before if before else visible
        pool.sort(key=lambda d: (len(d["scope"]), d["line"]))
        return pool[-1]

    def def_at(self, line, character):
        """A definition whose name covers the 0-based position."""
        for d in self.defs:
            if d["line"] == line and d["col"] - 1 <= character <= \
                    d["col"] - 1 + len(d["name"]):
                return d
        return None

    def ref_at(self, line, character):
        """A reference whose name covers the 0-based position."""
        for r in self.refs:
            if r["line"] == line and r["col"] - 1 <= character <= \
                    r["col"] - 1 + len(r["name"]):
                return r
        return None


def _analyze(source, filename):
    """Parse and analyze *source*; returns _Analyzer or None on error."""
    try:
        return _Analyzer(source, filename)
    except KabirError:
        return None


# ----------------------------------------------------------------------
# The server.
# ----------------------------------------------------------------------

class LanguageServer:
    """A Kabir language server: JSON-RPC over stdio, Content-Length framing.

    ``handle_message`` takes one decoded message dict and returns the
    list of outgoing message dicts (a response, a notification such as
    publishDiagnostics, or nothing). ``serve`` loops over a pair of
    binary streams until EOF or an ``exit`` notification, and returns
    the process exit code (0 after shutdown, 1 if exit came first).
    """

    def __init__(self):
        self.documents = {}  # uri -> {"text": str, "version": int|None}
        self.shutdown_received = False
        self.exit_code = None
        self._initialized = False

    # -- transport -----------------------------------------------------
    def serve(self, in_stream=None, out_stream=None):
        in_stream = in_stream if in_stream is not None else sys.stdin.buffer
        out_stream = out_stream if out_stream is not None else sys.stdout.buffer
        while True:
            try:
                message = read_message(in_stream)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                write_message(out_stream, self._error_response(
                    None, PARSE_ERROR, "Parse error"))
                continue
            if message is None:
                break
            for outgoing in self.handle_message(message):
                write_message(out_stream, outgoing)
            if self.exit_code is not None:
                break
        return self.exit_code if self.exit_code is not None else 0

    # -- dispatch ------------------------------------------------------
    def handle_message(self, message):
        if not isinstance(message, dict):
            return []
        method = message.get("method")
        msg_id = message.get("id")
        is_request = "id" in message
        if method is None:
            return []  # a response to a request we never sent; ignore
        handler = {
            "initialize": self._on_initialize,
            "initialized": self._on_initialized,
            "shutdown": self._on_shutdown,
            "exit": self._on_exit,
            "textDocument/didOpen": self._on_did_open,
            "textDocument/didChange": self._on_did_change,
            "textDocument/didClose": self._on_did_close,
            "textDocument/completion": self._on_completion,
            "textDocument/hover": self._on_hover,
            "textDocument/definition": self._on_definition,
            "textDocument/formatting": self._on_formatting,
        }.get(method)
        if handler is None:
            if is_request:
                return [self._error_response(
                    msg_id, METHOD_NOT_FOUND,
                    "Method not found: %s" % method)]
            return []  # unknown notification: ignore, never crash
        try:
            result = handler(message.get("params") or {})
        except ValueError as exc:
            # Handlers raise ValueError for bad client input (e.g.
            # formatting a file that does not parse).
            if is_request:
                return [self._error_response(
                    msg_id, INVALID_PARAMS, str(exc))]
            return []
        except Exception as exc:  # never let one message kill the server
            if is_request:
                return [self._error_response(
                    msg_id, INTERNAL_ERROR,
                    "Internal error: %s" % exc)]
            return []
        if not is_request:
            return result if isinstance(result, list) else []
        return [self._result_response(msg_id, result)]

    # -- message helpers -----------------------------------------------
    @staticmethod
    def _result_response(msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error_response(msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    @staticmethod
    def _notification(method, params):
        return {"jsonrpc": "2.0", "method": method, "params": params}

    # -- lifecycle -----------------------------------------------------
    def _on_initialize(self, _params):
        self._initialized = True
        return {
            "capabilities": {
                "textDocumentSync": 1,  # full document sync
                "completionProvider": {"triggerCharacters": ["."]},
                "hoverProvider": True,
                "definitionProvider": True,
                "documentFormattingProvider": True,
            },
            "serverInfo": {"name": "kabir-lsp", "version": __version__},
        }

    def _on_initialized(self, _params):
        return []

    def _on_shutdown(self, _params):
        self.shutdown_received = True
        return None

    def _on_exit(self, _params):
        self.exit_code = 0 if self.shutdown_received else 1
        return []

    # -- documents -----------------------------------------------------
    def _publish_diagnostics(self, uri):
        doc = self.documents.get(uri)
        text = doc["text"] if doc is not None else ""
        return [self._notification("textDocument/publishDiagnostics", {
            "uri": uri,
            "diagnostics": _diagnostics(uri, text),
        })]

    def _on_did_open(self, params):
        doc = params.get("textDocument") or {}
        uri = doc.get("uri", "")
        self.documents[uri] = {"text": doc.get("text", ""),
                               "version": doc.get("version")}
        return self._publish_diagnostics(uri)

    def _on_did_change(self, params):
        doc = params.get("textDocument") or {}
        uri = doc.get("uri", "")
        if uri not in self.documents:
            self.documents[uri] = {"text": "", "version": None}
        for change in params.get("contentChanges") or []:
            if "range" in change:
                self.documents[uri]["text"] = _apply_range_edit(
                    self.documents[uri]["text"], change["range"],
                    change.get("text", ""))
            else:
                self.documents[uri]["text"] = change.get("text", "")
        self.documents[uri]["version"] = doc.get("version")
        return self._publish_diagnostics(uri)

    def _on_did_close(self, params):
        doc = params.get("textDocument") or {}
        self.documents.pop(doc.get("uri", ""), None)
        return []

    # -- language features ---------------------------------------------
    def _on_completion(self, params):
        uri = (params.get("textDocument") or {}).get("uri", "")
        position = params.get("position") or {}
        line = position.get("line", 0)
        character = position.get("character", 0)
        doc = self.documents.get(uri)
        if doc is None:
            return {"isIncomplete": False, "items": []}
        text = doc["text"]
        lines = text.split("\n")
        line_text = lines[line] if 0 <= line < len(lines) else ""
        before = line_text[:character]
        prefix_match = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", before)
        prefix = prefix_match.group(0) if prefix_match else ""
        if before.rstrip().endswith("."):
            items = [{
                "label": name,
                "kind": 2,  # Method
                "detail": "string method",
            } for name in sorted(_STRING_METHODS)]
            return {"isIncomplete": False, "items": items}
        items = []
        seen = set()

        def add(label, kind, detail):
            if label not in seen:
                seen.add(label)
                items.append({"label": label, "kind": kind,
                              "detail": detail})

        analyzer = _analyze(text, uri)
        if analyzer is not None:
            for d in analyzer.defs:
                if d["line"] <= line + 1:
                    kind = 3 if d["kind"] == "function" else 6
                    add(d["name"], kind, d["detail"])
        for keyword in sorted(KEYWORDS):
            add(keyword, 14, "keyword")  # Keyword
        for name in sorted(_BUILTIN_RETURNS):
            add(name, 3, "builtin function -> %s"
                % _BUILTIN_RETURNS[name])  # Function
        for name in sorted(_STDLIB_RETURNS):
            add(name, 3, "stdlib function -> %s"
                % _STDLIB_RETURNS[name])
        for name in _TYPE_NAMES:
            add(name, 25, "type")  # TypeParameter
        if prefix:
            items = [i for i in items if i["label"].startswith(prefix)]
        return {"isIncomplete": False, "items": items}

    def _hover_info(self, uri, position):
        """(detail, type, def_line) for the word at *position*, or None."""
        line = position.get("line", 0)
        character = position.get("character", 0)
        doc = self.documents.get(uri)
        if doc is None:
            return None
        found = _word_at(doc["text"], line, character)
        if found is None:
            return None
        name = found[0]
        analyzer = _analyze(doc["text"], uri)
        if analyzer is not None:
            definition = analyzer.def_at(line + 1, character)
            if definition is None:
                ref = analyzer.ref_at(line + 1, character)
                if ref is not None:
                    definition = analyzer.resolve(ref["name"], ref["scope"],
                                                  ref["line"])
            if definition is not None:
                return (definition["detail"], definition["type"],
                        definition["line"])
        if name in _BUILTIN_RETURNS:
            return ("builtin %s(...) -> %s"
                    % (name, _BUILTIN_RETURNS[name]),
                    _BUILTIN_RETURNS[name], None)
        if name in _STDLIB_RETURNS:
            return ("stdlib %s(...) -> %s"
                    % (name, _STDLIB_RETURNS[name]),
                    _STDLIB_RETURNS[name], None)
        return None

    def _on_hover(self, params):
        uri = (params.get("textDocument") or {}).get("uri", "")
        info = self._hover_info(uri, params.get("position") or {})
        if info is None:
            return None
        detail, type_, def_line = info
        value = "```kabir\n%s\n```" % detail
        if def_line is not None:
            value += "\n\nDefined at line %d." % def_line
        elif type_ is not None:
            value += "\n\nType: %s." % type_
        return {"contents": {"kind": "markdown", "value": value}}

    def _on_definition(self, params):
        uri = (params.get("textDocument") or {}).get("uri", "")
        position = params.get("position") or {}
        line = position.get("line", 0)
        character = position.get("character", 0)
        doc = self.documents.get(uri)
        if doc is None:
            return None
        found = _word_at(doc["text"], line, character)
        if found is None:
            return None
        name = found[0]
        analyzer = _analyze(doc["text"], uri)
        if analyzer is None:
            return None
        definition = analyzer.def_at(line + 1, character)
        if definition is None:
            ref = analyzer.ref_at(line + 1, character)
            if ref is None:
                return None
            definition = analyzer.resolve(ref["name"], ref["scope"],
                                          ref["line"])
        if definition is None:
            return None
        start_line = definition["line"] - 1
        start_char = definition["col"] - 1
        return {
            "uri": uri,
            "range": {
                "start": {"line": start_line, "character": start_char},
                "end": {"line": start_line,
                        "character": start_char + len(definition["name"])},
            },
        }

    def _on_formatting(self, params):
        uri = (params.get("textDocument") or {}).get("uri", "")
        doc = self.documents.get(uri)
        if doc is None:
            return []
        text = doc["text"]
        try:
            formatted = format_source(text, uri)
        except KabirError as err:
            raise ValueError("Cannot format: %s" % err.message)
        if formatted == text:
            return []
        lines = text.split("\n")
        return [{
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": len(lines) - 1,
                        "character": len(lines[-1])},
            },
            "newText": formatted,
        }]


# ----------------------------------------------------------------------
# Diagnostics.
# ----------------------------------------------------------------------

def _diagnostics(uri, text):
    """Parse + type-check *text*; one diagnostic per first error."""
    lines = text.split("\n")
    try:
        program = parse_source(text, uri)
    except KabirError as err:
        return [_diagnostic(err, lines)]
    try:
        check_program(program, filename=uri, lines=lines)
    except KabirError as err:
        return [_diagnostic(err, lines)]
    return []


def _diagnostic(err, lines):
    line = (err.line or 1) - 1
    col = (err.col or 1) - 1
    line_len = len(lines[line]) if 0 <= line < len(lines) else col + 1
    return {
        "range": {
            "start": {"line": line, "character": col},
            "end": {"line": line,
                    "character": min(line_len, col + 1)},
        },
        "severity": 1,  # Error
        "source": "kabir",
        "message": err.message,
    }


def _apply_range_edit(text, range_, new_text):
    """Apply one LSP range edit (0-based line/character) to *text*."""
    lines = text.split("\n")
    start = range_["start"]
    end = range_["end"]

    def offset(line, character):
        return sum(len(lines[i]) + 1 for i in range(min(line, len(lines)))) \
            + character

    start_off = offset(start["line"], start["character"])
    end_off = offset(end["line"], end["character"])
    return text[:start_off] + new_text + text[end_off:]


def main():
    """Entry point for ``kabir lsp`` (wired by the coordinator in cli.py)."""
    return LanguageServer().serve()


if __name__ == "__main__":
    sys.exit(main())
