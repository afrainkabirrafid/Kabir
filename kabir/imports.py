"""Import resolution for Kabir (7.0.0, Leap 1: registry import wiring).

This is the ONE place that turns an ``import`` spec into a file on
disk. Every backend -- the tree-walking interpreter, the bytecode VM,
and the native backend -- must call :func:`resolve_import_spec`
(or the two helpers below it); do not build a second resolver.

Two kinds of import, told apart by the spec itself:

* ``import "path/to/file.kbr"`` -- a quoted file import. The path
  resolves relative to the importing file's directory (or the process
  working directory when there is no importing file, e.g. the REPL),
  exactly as before. A quoted spec always ends in ``.kbr``: the
  parser refuses anything else.
* ``import greet`` -- a bare-name package import. The name resolves
  through the enclosing project: ``Kabir.toml`` ``[dependencies]``
  gives the exact version wanted, ``Kabir.lock`` gives the exact
  version installed, and the entry point is
  ``vendor/<name>/<version>/src/main.kbr`` under the project root.
  Exact versions only -- a manifest/lock disagreement is an error,
  never a range resolution.

Transitive imports work the same way: a vendored package that itself
does ``import <its-dependency>`` resolves against the *consumer*
project's ``vendor/`` tree, not its own. The project root is found by
walking up from the importing file and skipping the vendored
package's own ``Kabir.toml`` (``vendor/<name>/<version>/Kabir.toml``),
so the consumer project's manifest + lockfile always win.

All failures raise :class:`kabir.errors.KabirError` with a message
that names the package and says how to fix it (usually one
``kabir add`` command). Callers convert that into a statement-level
error at the ``import`` line; this module never touches the AST.
"""

import os
from pathlib import Path

from .errors import KabirError
from .project import (LOCK_FILENAME, MAIN_SOURCE, MANIFEST_FILENAME,
                      NAME_RE, VENDOR_DIR, read_lockfile, read_manifest)


def is_package_spec(spec):
    """True when *spec* is a bare package name, not a quoted file path.

    The rule is the whole contract: quoted imports always end in
    ``.kbr`` (the parser enforces it) and package names never contain
    a dot (``NAME_RE`` forbids it), so the two forms can never collide.
    """
    return isinstance(spec, str) and bool(spec) and not spec.endswith(".kbr")


def resolve_import_spec(spec, importing_file):
    """Resolve an ``import`` spec to a canonical absolute file path.

    *spec* is the raw string from the ``IMPORT`` instruction / the
    ``ImportStmt`` node: either a ``.kbr`` path or a bare package
    name. *importing_file* is the file doing the importing (or a
    ``<...>`` pseudo-filename for the REPL / stdin). Returns the
    canonical (realpath) absolute path of the module's entry file.
    Raises :class:`KabirError` when the spec cannot be resolved.
    """
    if not isinstance(spec, str) or not spec:
        raise KabirError("Cannot import an empty name -- write "
                         'import "file.kbr" or import <package>')
    if is_package_spec(spec):
        return resolve_package_import(spec, importing_file)
    return resolve_file_import(spec, importing_file)


def resolve_file_import(path, importing_file):
    """Resolve a quoted ``.kbr`` import relative to its importer.

    Same rule as the 1.1.0 importer: relative to the importing file's
    directory, or the process working directory when there is no
    importing file (``<repl>``, ``<stdin>``, tests).
    """
    if importing_file.startswith("<"):
        base = os.getcwd()
    else:
        base = os.path.dirname(os.path.abspath(importing_file))
    resolved = os.path.join(base, path)
    return os.path.realpath(os.path.abspath(resolved))


def find_project_root(start_dir):
    """Walk up from *start_dir* to the enclosing project root.

    Returns the directory holding the project's ``Kabir.toml``, or
    ``None`` when there is no project above *start_dir*. A
    ``Kabir.toml`` inside ``vendor/<name>/<version>/`` belongs to a
    vendored package, not to the consumer project, so it is skipped:
    transitive imports resolve against the consumer's manifest and
    lockfile, never the package's own.
    """
    directory = Path(start_dir).resolve()
    while True:
        manifest = directory / MANIFEST_FILENAME
        if manifest.is_file():
            # Vendored package dir: <root>/vendor/<name>/<version>.
            # Its parent's parent is the "vendor" directory itself.
            if directory.parent.parent.name != VENDOR_DIR:
                return directory
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def resolve_package_import(name, importing_file):
    """Resolve a bare-name import via the project's manifest + lock.

    ``<project>/vendor/<name>/<version>/src/main.kbr`` is the entry
    point, where ``<version>`` must agree between ``Kabir.toml``
    ``[dependencies]`` and ``Kabir.lock`` (exact versions only).
    Raises :class:`KabirError` naming the package on every failure.
    """
    if not NAME_RE.match(name):
        raise KabirError(
            'Invalid package name "%s" -- package names must start with '
            'a letter and contain only letters, digits, "-" and "_"'
            % name)
    if importing_file.startswith("<"):
        start_dir = os.getcwd()
    else:
        start_dir = os.path.dirname(os.path.abspath(importing_file))
    project_dir = find_project_root(start_dir)
    if project_dir is None:
        raise KabirError(
            'Cannot import package "%s" -- no Kabir.toml found above %s; '
            "bare-name imports need a project, so run "
            "'kabir add %s@<version>' inside a project first"
            % (name, start_dir, name))
    manifest = read_manifest(project_dir / MANIFEST_FILENAME)
    dependencies = manifest["dependencies"]
    lock = read_lockfile(project_dir)
    entry = lock.get(name)
    if name in dependencies:
        # Direct dependency: the manifest's exact version must agree
        # with the lockfile's -- a mismatch means the install is stale.
        wanted = dependencies[name]
        if entry is None:
            raise KabirError(
                'Package "%s" is listed in Kabir.toml (%s) but missing '
                "from %s -- run 'kabir add %s@%s' to install it"
                % (name, wanted, LOCK_FILENAME, name, wanted))
        locked = entry.get("version")
        if locked != wanted:
            raise KabirError(
                'Version mismatch for package "%s" -- Kabir.toml asks '
                "for %s but %s has %s; run 'kabir add %s@%s' to "
                "reinstall it"
                % (name, wanted, LOCK_FILENAME, locked, name, wanted))
    elif entry is not None:
        # Transitive dependency: installed (and locked) by the package
        # that needs it, but not a direct dependency of this project.
        # The lockfile is the only version there is -- use it.
        locked = entry.get("version")
    else:
        raise KabirError(
            'Package "%s" is not a dependency of project "%s" -- run '
            "'kabir add %s@<version>' in %s"
            % (name, manifest["name"], name, project_dir))
    main = project_dir / VENDOR_DIR / name / locked / MAIN_SOURCE
    if not main.is_file():
        raise KabirError(
            'Package "%s" %s is locked but %s is missing -- run '
            "'kabir add %s@%s' to reinstall it"
            % (name, locked, main, name, locked))
    return os.path.realpath(str(main))
