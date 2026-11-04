"""Friendly error reporting for Kabir.

Every Kabir error carries a file name, a line number, the offending
source line, and a caret pointing at the problem -- never a raw
Python traceback.
"""


class KabirError(Exception):
    """A user-facing Kabir error with source context."""

    def __init__(self, message, filename="<stdin>", line=None, col=None,
                 source_line=None):
        super().__init__(message)
        self.message = message
        self.filename = filename or "<stdin>"
        self.line = line
        self.col = col
        self.source_line = source_line

    def render(self):
        """Render the friendly multi-line error message."""
        parts = []
        if self.line is not None:
            loc = "%s:%d" % (self.filename, self.line)
            if self.col is not None:
                loc += ":%d" % self.col
            parts.append("%s: %s" % (loc, self.message))
        else:
            parts.append("%s: %s" % (self.filename, self.message))
        if self.source_line is not None:
            parts.append("  %s" % self.source_line.rstrip("\n"))
            if self.col is not None:
                parts.append("  %s^" % (" " * max(0, self.col - 1)))
            else:
                parts.append("  ^")
        return "\n".join(parts)

    def __str__(self):
        return self.render()


def error_at(message, filename, lines, line, col=None):
    """Build a KabirError for a position in *lines* (list of source lines)."""
    source_line = None
    if lines and line is not None and 1 <= line <= len(lines):
        source_line = lines[line - 1]
    return KabirError(message, filename=filename, line=line, col=col,
                      source_line=source_line)
