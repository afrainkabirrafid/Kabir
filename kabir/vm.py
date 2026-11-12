"""Stack VM for Kabir 2.0.0.

Executes the bytecode produced by ``kabir/compile.py``. Frames hold
an env chain; the value stack is one list shared across frames, each
frame recording its base so CALL/RETURN slice correctly. Builtins run
through the ``(interp, node, args)`` protocol unchanged: the VM
exposes ``.error(node, msg)``, ``.format_value(v)`` and ``._random``.

4.0.0 async: the VM is also a cooperative scheduler. Each task owns
its own frames + value stack; the scheduler round-robins tasks in
fixed quanta. ``await`` suspends the current task until the awaited
task completes; ``recv`` on an empty channel suspends it until a
``send`` arrives. A run waits for every spawned task; if the only
live tasks are blocked with nothing able to wake them, the run
fails with a deadlock error instead of hanging.
"""

import collections
import math
import os
import random

from . import ast_nodes as ast  # noqa: F401  (node shapes, for reference)
from . import semantics as _semantics
from .builtins import BuiltinFunction, _type_name
from .compile import compile_program, compile_source
from .errors import KabirError, error_at
from .interpreter import Environment
from .parser import parse as _parse_source
from .stdlib import STRING_METHODS, build_stdlib
from .builtins import build_builtins


def _article(word):
    return "an" if word[:1] in "aeiou" else "a"


# Sentinel for a slot that has not been DEFINE_SLOT'd yet. Distinct
# from Kabir null (Python None) so an uninitialised slot errors
# instead of silently reading as null.
_UNSET = object()


class _CallNode:
    """Tiny node shim for builtin calls: carries line + filename."""

    def __init__(self, line, filename, col=None):
        self.line = line
        self.col = col
        self.filename = filename


class VMFunction:
    """A user-defined Kabir function value (a closure over bytecode)."""

    def __init__(self, name, params, body, closure_env, is_async=False):
        self.name = name or "<anonymous>"
        self.params = params
        self.body = body  # a code dict
        self.closure_env = closure_env
        # 4.0.0 async: calling an async function starts a task and
        # returns it at once instead of running the body inline.
        self.is_async = is_async

    def __repr__(self):
        return "<function %s>" % self.name


class _TaskSuspend(Exception):
    """Internal: the current task cannot proceed yet.

    Raised by the AWAIT instruction (waiting on another task) and by
    ``recv`` (waiting on a channel). Carries what the task waits on;
    the task runner registers the wait and suspends the task. Never
    escapes the VM -- user code only ever sees KabirError.
    """

    def __init__(self, kind, obj):
        super().__init__(kind)
        self.kind = kind  # "channel" | "task"
        self.obj = obj


class Task:
    """One cooperative task: its own frames + value stack.

    ``state`` is ready | running | blocked | done | failed.
    ``waiters`` are tasks suspended in ``await`` on this task; they
    are woken with this task's return value (or its error) when it
    finishes. ``waiting_on`` is (kind, obj) while blocked.
    ``resume_error`` is a KabirError to raise in this task the next
    time the scheduler runs it (an awaited task failed).
    """

    _kabir_type = "task"

    def __init__(self, task_id, name):
        self.id = task_id
        self.name = name or "<anonymous>"
        self.frames = []
        self.stack = []
        self.state = "ready"
        self.result = None
        self.error = None
        self.waiters = []
        self.waiting_on = None
        self.resume_error = None

    def __repr__(self):
        return "<task %d>" % self.id


class Channel:
    """An unbounded FIFO channel shared between tasks.

    ``queue`` holds values sent with no receiver waiting (a deque so
    send/recv stay O(1)); ``waiters`` holds tasks blocked in ``recv``
    (also FIFO, so the longest-waiting receiver gets the next send).
    """

    _kabir_type = "channel"

    def __init__(self):
        self.queue = collections.deque()
        self.waiters = collections.deque()

    def __repr__(self):
        return "<channel>"


class _Frame:
    __slots__ = ("code", "ip", "stack_base", "env", "module_info", "slots")

    def __init__(self, code, stack_base, env, module_info=None):
        self.code = code
        self.ip = 0
        self.stack_base = stack_base
        self.env = env
        # Slot storage for this frame, one entry per name in
        # code["locals"] (v2 code dicts; v1 dicts have no "locals", so
        # the list is empty and every variable stays name-based).
        # Entries start as _UNSET so reading a slot before its
        # DEFINE_SLOT runs reports "Undefined variable" instead of
        # silently returning null.
        self.slots = [_UNSET] * len(code.get("locals") or [])
        # module_info: (canonical_path, module_env, importer_env) for a
        # frame running an imported module; None otherwise.
        self.module_info = module_info


class VM:
    MAX_CALL_DEPTH = 500
    # 4.0.0 async: instructions one task runs before the scheduler
    # gives another task a turn, and the ceiling on live tasks.
    QUANTUM = 1000
    MAX_TASKS = 1000

    def __init__(self, source="", filename="<stdin>"):
        self.source = source
        self.filename = filename
        self.lines = source.split("\n") if source else []
        self.globals = Environment()
        self.modules = {}    # canonical path -> exported-bindings dict
        self._importing = []  # canonical paths currently being executed
        for name, fn in build_builtins().items():
            self.globals.define(name, fn)
        self._random = random.Random()
        for name, fn in build_stdlib().items():
            self.globals.define(name, fn)
        self.globals.define("pi", math.pi)
        self.globals.define("e", math.e)
        self._file_lines = {}
        if filename:
            self._file_lines[filename] = self.lines
        self._frames = []
        self._stack = []
        # 4.0.0 async scheduler state (reset per run_code call):
        # _ready: FIFO of tasks that can run; _blocked: tasks waiting
        # on a channel or another task; _failed: tasks that raised
        # (reported at the end of the run if nobody awaited them);
        # _live_count: ready + running + blocked tasks;
        # _current_task: the task a builtin call belongs to.
        self._ready = collections.deque()
        self._blocked = set()
        self._failed = []
        self._live_count = 0
        self._next_task_id = 0
        self._current_task = None

    # -- errors --------------------------------------------------------
    def error(self, node, message):
        line = getattr(node, "line", None)
        col = getattr(node, "col", None)
        filename = getattr(node, "filename", None) \
            or self._current_filename()
        lines = self._file_lines.get(filename) or self.lines
        raise error_at(message, filename, lines, line, col)

    def _current_filename(self):
        if self._frames:
            return self._frames[-1].code.get("filename") or self.filename
        return self.filename

    def _instr_error(self, frame, line, col, message):
        filename = frame.code.get("filename") or self.filename
        lines = self._file_lines.get(filename) or self.lines
        raise error_at(message, filename, lines, line, col)

    # -- shared semantics ----------------------------------------------
    def format_value(self, value):
        return _semantics.format_value(value)

    @staticmethod
    def is_truthy(value):
        return _semantics.is_truthy(value)

    # -- running -------------------------------------------------------
    def run_source(self, source, filename="<stdin>"):
        """Parse, compile and run source text."""
        self.source = source
        self.filename = filename
        self.lines = source.split("\n")
        self._file_lines[filename] = self.lines
        code = compile_source(source, filename)
        return self.run_code(code, filename=filename, lines=self.lines)

    def run_code(self, code, filename=None, lines=None):
        """Run an already-compiled code dict. Returns the main value."""
        filename = filename or code.get("filename") or self.filename
        if lines is not None:
            self._file_lines[filename] = lines
        elif filename in self._file_lines:
            lines = self._file_lines[filename]
        # The top level is itself a task (task 1); spawned tasks are
        # numbered from 2. A run waits for every task (see _schedule).
        self._ready = collections.deque()
        self._blocked = set()
        self._failed = []
        self._live_count = 0
        self._next_task_id = 0
        self._current_task = None
        main = self._new_task("<main>")
        main.frames = [_Frame(code, 0, self.globals)]
        self._ready.append(main)
        self._stack = main.stack
        self._frames = main.frames
        self._importing = []
        try:
            return self._schedule(main)
        except KabirError:
            self._importing = []
            raise

    # -- scheduler (4.0.0 async) --------------------------------------
    def _new_task(self, name, node=None, frame=None, line=None, col=None):
        """Create a task (not yet queued). Counts against MAX_TASKS."""
        if self._live_count >= self.MAX_TASKS:
            message = ("Too many live tasks -- the limit is %d. A loop "
                       "that spawns a task on every iteration will hit "
                       "it; spawn fewer tasks, or await them as you go"
                       % self.MAX_TASKS)
            if node is not None:
                self.error(node, message)
            if frame is not None:
                self._instr_error(frame, line, col, message)
            raise KabirError(message, filename=self.filename)
        self._live_count += 1
        self._next_task_id += 1
        return Task(self._next_task_id, name)

    def _schedule(self, main):
        """Run tasks round-robin until every task is done.

        Returns the main task's return value. A background task that
        fails raises its error here once nothing else can run (or
        sooner, through an awaiter that re-raises it); tasks that can
        never wake raise a deadlock error instead of hanging forever.
        """
        while self._ready or self._blocked:
            if not self._ready:
                self._deadlock_error()
            task = self._ready.popleft()
            task.state = "running"
            self._current_task = task
            self._stack = task.stack
            self._frames = task.frames
            try:
                status, value = self._run_task(task, self.QUANTUM)
            except KabirError as err:
                task.state = "failed"
                task.error = err
                self._live_count -= 1
                self._failed.append(task)
                self._current_task = None
                self._wake_waiters_with_error(task, err)
                if task is main:
                    raise
                continue
            self._current_task = None
            if status == "done":
                task.state = "done"
                task.result = value
                self._live_count -= 1
                self._wake_waiters(task, value)
            elif status == "blocked":
                task.state = "blocked"
                self._blocked.add(task)
            else:  # quantum expired: back of the queue
                task.state = "ready"
                self._ready.append(task)
        if self._failed:
            raise self._failed[0].error
        return main.result

    def _wake_waiters(self, task, value):
        """Resume every task awaiting *task*, delivering its result."""
        waiters, task.waiters = task.waiters, []
        for waiter in waiters:
            waiter.waiting_on = None
            waiter.state = "ready"
            # The AWAIT already consumed its operands; the result is
            # the await expression's value.
            waiter.stack.append(value)
            self._blocked.discard(waiter)
            self._ready.append(waiter)

    def _wake_waiters_with_error(self, task, err):
        """Resume every task awaiting *task*, raising its error there."""
        waiters, task.waiters = task.waiters, []
        for waiter in waiters:
            waiter.waiting_on = None
            waiter.state = "ready"
            waiter.resume_error = err
            self._blocked.discard(waiter)
            self._ready.append(waiter)

    def _deadlock_error(self):
        """No task can run and some are blocked: report the first."""
        task = next(iter(self._blocked))
        kind, obj = task.waiting_on or (None, None)
        if kind == "channel":
            detail = ("waiting to receive from a channel, but no live "
                      "task can send to it -- check that a sender is "
                      "still running")
        elif kind == "task":
            detail = ("waiting for task %d (%s), which is itself "
                      "blocked and can never finish"
                      % (obj.id, obj.name))
        else:
            detail = "blocked, but no live task can wake it"
        frame = task.frames[-1] if task.frames else None
        if frame is not None and frame.ip > 0:
            # ip already points past the blocking instruction.
            pos = frame.ip - 1
            line = frame.code["lines"][pos]
            cols = frame.code.get("cols")
            col = cols[pos] if cols and pos < len(cols) else None
            filename = frame.code.get("filename") or self.filename
            lines = self._file_lines.get(filename) or self.lines
            raise error_at("Deadlock -- task %d (%s) is %s"
                           % (task.id, task.name, detail),
                           filename, lines, line, col)
        raise KabirError("Deadlock -- task %d (%s) is %s"
                         % (task.id, task.name, detail),
                         filename=self.filename)

    # -- main loop -----------------------------------------------------
    def _run_task(self, task, quantum):
        """Run one task for up to *quantum* instructions.

        Returns ("done", value) when the task's frames empty,
        ("blocked", None) when it suspended on a channel or a task,
        ("quantum", None) when its quantum expired. A KabirError
        propagates to the scheduler, which fails the task.
        ``self._stack`` / ``self._frames`` are the task's own lists
        for the duration of the call, so every helper that touches
        them (calls, imports, finishing a frame) works unchanged.
        """
        if task.resume_error is not None:
            err, task.resume_error = task.resume_error, None
            raise err
        stack = task.stack
        frames = task.frames
        # Dispatch table, built once per VM: looking up "_op_" + op with
        # getattr on every instruction was the single hottest cost.
        dispatch = self.__dict__.get("_dispatch")
        if dispatch is None:
            dispatch = self._dispatch = {
                name[4:]: getattr(self, name)
                for name in dir(self) if name.startswith("_op_")
            }
        frame = None
        instrs = instr_lines = instr_cols = None
        steps = 0
        while frames:
            cur = frames[-1]
            if cur is not frame:
                # Frame changed (call / return): re-read its code.
                frame = cur
                code = frame.code
                instrs = code["instrs"]
                instr_lines = code["lines"]
                # v1 code dicts have no "cols"; col is then None and
                # errors print file:line exactly as before.
                instr_cols = code.get("cols")
            if frame.ip >= len(instrs):
                value = self._finish_frame(frame, None)
                if value is _MAIN_DONE:
                    return ("done", None)
                frame = None
                continue
            op, arg = instrs[frame.ip]
            line = instr_lines[frame.ip]
            col = instr_cols[frame.ip] \
                if instr_cols and frame.ip < len(instr_cols) else None
            frame.ip += 1
            handler = dispatch.get(op)
            if handler is None:
                self._instr_error(frame, line, col, "Unknown instruction %r" % op)
            try:
                result = handler(frame, arg, line, col, stack, frames)
            except _TaskSuspend as susp:
                # Register the wait, then suspend: the scheduler
                # wakes this task when the channel / task delivers.
                task.waiting_on = (susp.kind, susp.obj)
                if susp.kind == "channel":
                    susp.obj.waiters.append(task)
                else:
                    susp.obj.waiters.append(task)
                return ("blocked", None)
            if isinstance(result, tuple) and result[0] is _MAIN_DONE:
                return ("done", result[1])
            steps += 1
            if steps >= quantum:
                return ("quantum", None)
        return ("done", None)

    def _finish_frame(self, frame, value):
        """Pop a frame; return _MAIN_DONE if that was the main frame."""
        stack = self._stack
        frames = self._frames
        del stack[frame.stack_base:]
        frames.pop()
        if frame.module_info is not None:
            canonical, module_env, importer_env = frame.module_info
            if canonical in self._importing:
                self._importing.remove(canonical)
            exports = dict(module_env.vars)
            self.modules[canonical] = exports
            for name, val in exports.items():
                importer_env.define(name, val)
            return None  # module return value is discarded
        if not frames:
            return _MAIN_DONE
        stack.append(value)
        return None

    # -- helpers -------------------------------------------------------
    def _function_frame(self, callee, args, stack_base):
        """Build the entry frame for calling *callee* with *args*.

        Caller must have checked arity first. Parameters that are
        slot-allocated (code["locals"][i] is the parameter's own
        name) go into the frame's slots; a captured parameter is
        defined in the env so nested functions can find it through
        the env chain -- exactly as the sync call path did before
        this helper existed.
        """
        env = Environment(callee.closure_env)
        new_frame = _Frame(callee.body, stack_base, env)
        locals_list = callee.body.get("locals") or []
        for i, (name, value) in enumerate(zip(callee.params, args)):
            if i < len(locals_list) and locals_list[i] == name:
                new_frame.slots[i] = value
            else:
                env.define(name, value)
        return new_frame

    def _call_value(self, callee, args, frame, line, col, frames):
        """Call a value; pushes the result or a new frame. Returns True
        when a frame was pushed (caller must not push a result)."""
        stack = self._stack
        if isinstance(callee, BuiltinFunction):
            node = _CallNode(line, frame.code.get("filename")
                             or self.filename, col)
            stack.append(callee.impl(self, node, args))
            return False
        if isinstance(callee, VMFunction):
            if len(args) != len(callee.params):
                self._instr_error(
                    frame, line, col,
                    "Function '%s' takes %d argument%s, but got %d"
                    % (callee.name, len(callee.params),
                       "" if len(callee.params) == 1 else "s", len(args)))
            if callee.is_async:
                # 4.0.0 async: calling an async function starts a
                # task and returns it at once; the caller's task
                # keeps running. `await` the task to get the
                # function's return value. No frame is pushed here.
                task = self._new_task(callee.name, frame=frame,
                                      line=line, col=col)
                task.frames.append(self._function_frame(callee, args, 0))
                self._ready.append(task)
                stack.append(task)
                return False
            if len(frames) - 1 >= self.MAX_CALL_DEPTH:
                self._instr_error(
                    frame, line, col,
                    "Too many nested function calls -- check for "
                    "infinite recursion")
            frames.append(self._function_frame(callee, args, len(stack)))
            return True
        if isinstance(callee, str):
            self._instr_error(
                frame, line, col,
                "'%s' is a string, not a function -- check that you "
                "are calling the right name" % callee)
        self._instr_error(
            frame, line, col,
            "This value (%s) is not a function, so it cannot be called"
            % _type_name(callee))

    def _binary(self, frame, line, col, op, stack):
        right = stack.pop()
        left = stack.pop()
        try:
            stack.append(_semantics.binary_op(op, left, right))
        except _semantics.KabirRuntimeError as exc:
            self._instr_error(frame, line, col,
                              exc.args[0] if exc.args else str(exc))

    # -- async helpers (called by the spawn/chan/send/recv builtins) --
    def _async_new_channel(self, node):
        return Channel()

    def _async_send(self, channel, value, node):
        if not isinstance(channel, Channel):
            self.error(node, "send() needs a channel from chan(), not %s"
                       % _type_name(channel))
        if channel.waiters:
            # A task is blocked in recv: hand it the value at once
            # and wake it. The CALL already consumed its operands,
            # so the pushed value IS the recv() result.
            waiter = channel.waiters.popleft()
            waiter.waiting_on = None
            waiter.state = "ready"
            waiter.stack.append(value)
            self._blocked.discard(waiter)
            self._ready.append(waiter)
        else:
            channel.queue.append(value)
        return None

    def _async_recv(self, channel, node):
        if not isinstance(channel, Channel):
            self.error(node, "recv() needs a channel from chan(), not %s"
                       % _type_name(channel))
        if channel.queue:
            return channel.queue.popleft()
        # Nothing to receive: suspend this task until a send wakes
        # it. The scheduler registers the wait (see _run_task).
        raise _TaskSuspend("channel", channel)

    def _async_spawn_value(self, value, node):
        """spawn() reached as a plain builtin call (through a
        variable, so the compiler could not see the call): spawn a
        bare function value with no arguments."""
        if not isinstance(value, VMFunction):
            self.error(node, "spawn() needs a call, like spawn(work()) "
                       "-- got %s" % _type_name(value))
        if value.params:
            self.error(node, "spawn() through a variable cannot pass "
                       "arguments -- call it directly, like "
                       "spawn(%s(...))" % value.name)
        task = self._new_task(value.name, node=node)
        task.frames.append(self._function_frame(value, [], 0))
        self._ready.append(task)
        return task

    # -- imports -------------------------------------------------------
    def _load_module(self, path, frame, line, col, frames):
        cur_filename = frame.code.get("filename") or self.filename
        if cur_filename.startswith("<"):
            base = os.getcwd()
        else:
            base = os.path.dirname(os.path.abspath(cur_filename))
        resolved = os.path.join(base, path)
        canonical = os.path.realpath(os.path.abspath(resolved))
        if canonical in self._importing:
            chain = [os.path.basename(p) for p in self._importing]
            chain.append(os.path.basename(canonical))
            self._instr_error(frame, line, col, "Import cycle detected: %s"
                              % " -> ".join(chain))
        if canonical in self.modules:
            for name, value in self.modules[canonical].items():
                frame.env.define(name, value)
            return
        try:
            with open(canonical, "r", encoding="utf-8") as handle:
                source = handle.read()
        except FileNotFoundError:
            self._instr_error(frame, line, col,
                              'Cannot import "%s" -- no such file' % path)
        except UnicodeDecodeError:
            self._instr_error(frame, line, col,
                              'Cannot import "%s" -- the file is not '
                              'valid UTF-8' % path)
        except OSError as exc:
            self._instr_error(frame, line, col,
                              'Cannot import "%s" -- %s' % (path, exc))
        program = _parse_source(source, canonical)
        module_lines = source.split("\n")
        self._file_lines[canonical] = module_lines
        code = compile_program(program, canonical, module_lines)
        module_env = Environment(self.globals)
        self._importing.append(canonical)
        frames.append(_Frame(code, len(self._stack), module_env,
                             module_info=(canonical, module_env,
                                          frame.env)))

    # -- instructions --------------------------------------------------
    # Each _op_* takes (frame, arg, line, col, stack, frames). RETURN and
    # module completion are handled through _finish_frame; _MAIN_DONE
    # (a sentinel) ends the whole run.

    def _op_LOAD_CONST(self, frame, arg, line, col, stack, frames):
        stack.append(frame.code["consts"][arg])

    def _op_LOAD_NULL(self, frame, arg, line, col, stack, frames):
        stack.append(None)

    def _op_LOAD_TRUE(self, frame, arg, line, col, stack, frames):
        stack.append(True)

    def _op_LOAD_FALSE(self, frame, arg, line, col, stack, frames):
        stack.append(False)

    def _op_LOAD_VAR(self, frame, arg, line, col, stack, frames):
        if frame.env.has(arg):
            stack.append(frame.env.lookup(arg))
        else:
            self._instr_error(
                frame, line, col,
                "Undefined variable '%s' -- it was never defined "
                "with 'let'. Check the spelling" % arg)

    def _op_DEFINE_VAR(self, frame, arg, line, col, stack, frames):
        frame.env.define(arg, stack.pop())

    def _op_STORE_VAR(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        if not frame.env.assign(arg, value):
            self._instr_error(
                frame, line, col,
                "Cannot assign to '%s' -- it was never defined. "
                "Use 'let %s = ...' first" % (arg, arg))

    def _slot_name(self, frame, arg):
        locals_list = frame.code.get("locals") or []
        if 0 <= arg < len(locals_list) and locals_list[arg] is not None:
            return locals_list[arg]
        return None

    def _op_LOAD_SLOT(self, frame, arg, line, col, stack, frames):
        if arg < len(frame.slots):
            value = frame.slots[arg]
        else:
            value = _UNSET
        if value is _UNSET:
            name = self._slot_name(frame, arg) or ("slot %d" % arg)
            self._instr_error(
                frame, line, col,
                "Undefined variable '%s' -- it was never defined "
                "with 'let'. Check the spelling" % name)
        stack.append(value)

    def _op_DEFINE_SLOT(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        if arg >= len(frame.slots):
            frame.slots.extend([_UNSET] * (arg - len(frame.slots) + 1))
        frame.slots[arg] = value

    def _op_STORE_SLOT(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        if arg >= len(frame.slots) or frame.slots[arg] is _UNSET:
            name = self._slot_name(frame, arg) or ("slot %d" % arg)
            self._instr_error(
                frame, line, col,
                "Cannot assign to '%s' -- it was never defined. "
                "Use 'let %s = ...' first" % (name, name))
        frame.slots[arg] = value

    def _op_POP(self, frame, arg, line, col, stack, frames):
        stack.pop()

    def _op_DUP(self, frame, arg, line, col, stack, frames):
        stack.append(stack[-1])

    def _op_BUILD_LIST(self, frame, arg, line, col, stack, frames):
        if arg:
            items = stack[-arg:]
            del stack[-arg:]
        else:
            items = []
        stack.append(list(items))

    def _op_BUILD_DICT(self, frame, arg, line, col, stack, frames):
        result = {}
        if arg:
            flat = stack[-2 * arg:]
            del stack[-2 * arg:]
            for i in range(arg):
                result[flat[2 * i]] = flat[2 * i + 1]
        stack.append(result)

    def _op_BUILD_STRING(self, frame, arg, line, col, stack, frames):
        parts = stack[-arg:] if arg else []
        if arg:
            del stack[-arg:]
        out = []
        for part in parts:
            out.append(part if isinstance(part, str)
                       else self.format_value(part))
        stack.append("".join(out))

    def _op_ADD(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "+", stack)

    def _op_SUB(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "-", stack)

    def _op_MUL(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "*", stack)

    def _op_DIV(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "/", stack)

    def _op_MOD(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "%", stack)

    def _op_POW(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "**", stack)

    def _op_NEG(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._instr_error(frame, line, col,
                              "Cannot negate %s -- '-' needs a number"
                              % _type_name(value))
        stack.append(-value)

    def _op_NOT(self, frame, arg, line, col, stack, frames):
        stack.append(not _semantics.is_truthy(stack.pop()))

    def _op_EQ(self, frame, arg, line, col, stack, frames):
        right = stack.pop()
        left = stack.pop()
        stack.append(_semantics.kabir_equal(left, right))

    def _op_NE(self, frame, arg, line, col, stack, frames):
        right = stack.pop()
        left = stack.pop()
        stack.append(not _semantics.kabir_equal(left, right))

    def _op_LT(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "<", stack)

    def _op_LE(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, "<=", stack)

    def _op_GT(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, ">", stack)

    def _op_GE(self, frame, arg, line, col, stack, frames):
        self._binary(frame, line, col, ">=", stack)

    def _op_JUMP(self, frame, arg, line, col, stack, frames):
        frame.ip = arg

    def _op_JUMP_IF_FALSE(self, frame, arg, line, col, stack, frames):
        if not _semantics.is_truthy(stack.pop()):
            frame.ip = arg

    def _op_JUMP_IF_TRUE(self, frame, arg, line, col, stack, frames):
        if _semantics.is_truthy(stack.pop()):
            frame.ip = arg

    def _op_CALL(self, frame, arg, line, col, stack, frames):
        if arg:
            args = stack[-arg:]
            del stack[-arg:]
        else:
            args = []
        callee = stack.pop()
        self._call_value(callee, args, frame, line, col, frames)

    def _op_AWAIT(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        if not isinstance(value, Task):
            self._instr_error(
                frame, line, col,
                "'await' needs a task -- got %s. Call an async func "
                "or spawn() to get a task, like: let t = spawn(work())"
                % _type_name(value))
        if value is self._current_task:
            self._instr_error(
                frame, line, col,
                "A task cannot await itself -- it would wait forever")
        if value.state == "done":
            stack.append(value.result)
            return
        if value.state == "failed":
            # The awaited task raised: the error surfaces here, in
            # the awaiting task, at the await's position.
            raise value.error
        # Not finished yet: suspend until it completes (the runner
        # registers this task in value.waiters).
        raise _TaskSuspend("task", value)

    def _op_SPAWN_TASK(self, frame, arg, line, col, stack, frames):
        # Stack: [callee, arg1..argN] (compiled by the spawn()
        # special case in the compiler, so the call never ran in
        # this task). Pushes the new task as the spawn() result.
        if arg:
            args = stack[-arg:]
            del stack[-arg:]
        else:
            args = []
        callee = stack.pop()
        if not isinstance(callee, VMFunction):
            self._instr_error(
                frame, line, col,
                "spawn() needs a function call, like spawn(work()) "
                "-- got %s" % _type_name(callee))
        if len(args) != len(callee.params):
            self._instr_error(
                frame, line, col,
                "Function '%s' takes %d argument%s, but got %d"
                % (callee.name, len(callee.params),
                   "" if len(callee.params) == 1 else "s", len(args)))
        task = self._new_task(callee.name, frame=frame,
                              line=line, col=col)
        task.frames.append(self._function_frame(callee, args, 0))
        self._ready.append(task)
        stack.append(task)

    def _op_RETURN(self, frame, arg, line, col, stack, frames):
        value = stack.pop() if len(stack) > frame.stack_base else None
        result = self._finish_frame(frame, value)
        if result is _MAIN_DONE:
            # Signal the loop to stop with the main return value.
            return (_MAIN_DONE, value)
        return None

    def _op_MAKE_FUNCTION(self, frame, arg, line, col, stack, frames):
        const = frame.code["consts"][arg]
        code = const["__code__"]
        stack.append(VMFunction(code["name"], code["params"], code,
                                frame.env,
                                code.get("is_async", False)))

    def _op_IMPORT(self, frame, arg, line, col, stack, frames):
        self._load_module(arg, frame, line, col, frames)

    def _op_GET_INDEX(self, frame, arg, line, col, stack, frames):
        index = stack.pop()
        target = stack.pop()
        if isinstance(target, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line, col,
                    "An array index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self._instr_error(
                    frame, line, col,
                    "Array index %d is out of range -- the array "
                    "has %d item%s"
                    % (index, len(target),
                       "" if len(target) == 1 else "s"))
            stack.append(target[idx])
        elif isinstance(target, str):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line, col,
                    "A string index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(target) if index < 0 else index
            if idx < 0 or idx >= len(target):
                self._instr_error(
                    frame, line, col,
                    "String index %d is out of range -- the string "
                    "has %d character%s"
                    % (index, len(target),
                       "" if len(target) == 1 else "s"))
            stack.append(target[idx])
        elif isinstance(target, dict):
            if not isinstance(index, str):
                self._instr_error(
                    frame, line, col,
                    "A dict key must be a string, not %s"
                    % _type_name(index))
            if index not in target:
                self._instr_error(
                    frame, line, col,
                    "No key %r in this dict -- available keys: %s"
                    % (index, ", ".join('"%s"' % k for k in target.keys())
                       or "none"))
            stack.append(target[index])
        else:
            self._instr_error(
                frame, line, col,
                "Cannot index %s -- only arrays, strings and dicts "
                "support [ ... ]" % _type_name(target))

    def _op_SET_INDEX(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        index = stack.pop()
        container = stack.pop()
        if isinstance(container, list):
            if not isinstance(index, int) or isinstance(index, bool):
                self._instr_error(
                    frame, line, col,
                    "An array index must be a whole number, not %s"
                    % _type_name(index))
            idx = index + len(container) if index < 0 else index
            if idx < 0 or idx >= len(container):
                self._instr_error(
                    frame, line, col,
                    "Array index %d is out of range -- the array "
                    "has %d item%s"
                    % (index, len(container),
                       "" if len(container) == 1 else "s"))
            container[idx] = value
        elif isinstance(container, dict):
            if not isinstance(index, str):
                self._instr_error(
                    frame, line, col,
                    "A dict key must be a string, not %s"
                    % _type_name(index))
            container[index] = value
        else:
            self._instr_error(
                frame, line, col,
                "Cannot assign into %s -- only arrays and dicts "
                "support index assignment" % _type_name(container))
        stack.append(None)

    def _op_GET_MEMBER(self, frame, arg, line, col, stack, frames):
        receiver = stack.pop()
        if isinstance(receiver, str):
            if arg in STRING_METHODS:
                def bound(interp, node, args,
                          _name=arg, _recv=receiver):
                    return STRING_METHODS[_name](interp, node, _recv, args)
                stack.append(BuiltinFunction(arg, bound))
                return
            self._instr_error(
                frame, line, col,
                "No method '.%s' on a string -- strings have no "
                "method named '%s'" % (arg, arg))
        if isinstance(receiver, dict):
            self._instr_error(
                frame, line, col,
                "Cannot use .%s on a dict -- write dict[\"%s\"] "
                "instead (for example, user[\"name\"])" % (arg, arg))
        self._instr_error(
            frame, line, col,
            "Cannot use .%s on %s %s -- only strings have methods"
            % (arg, _article(_type_name(receiver)), _type_name(receiver)))

    def _op_ENTER_SCOPE(self, frame, arg, line, col, stack, frames):
        frame.env = Environment(frame.env)

    def _op_EXIT_SCOPE(self, frame, arg, line, col, stack, frames):
        if frame.env.parent is not None:
            frame.env = frame.env.parent

    def _op_GET_ITER(self, frame, arg, line, col, stack, frames):
        iterable = stack.pop()
        if isinstance(iterable, str):
            items = list(iterable)
        elif isinstance(iterable, list):
            items = list(iterable)
        elif isinstance(iterable, dict):
            items = list(iterable.keys())
        else:
            self._instr_error(
                frame, line, col,
                "Cannot loop over %s -- 'for' needs an array, a "
                "string, or a range" % _type_name(iterable))
        stack.append(iter(items))

    def _op_FOR_ITER(self, frame, arg, line, col, stack, frames):
        iterator = stack[-1]
        try:
            stack.append(next(iterator))
        except StopIteration:
            stack.pop()
            frame.ip = arg

    def _op_MATCH_FAIL(self, frame, arg, line, col, stack, frames):
        value = stack.pop()
        self._instr_error(frame, line, col,
                          "No pattern matched the value %s"
                          % self.format_value(value))


class _MainDoneType:
    pass


_MAIN_DONE = _MainDoneType()
