"""Cargo-style project tooling for Kabir (2.0.0).

Creates new projects (``kabir new``), builds them to bytecode
(``kabir build``), and resolves a project's entry point (``kabir run``).

It also implements the 3.0.0 package registry (``kabir add`` /
``kabir publish``), extended in 5.0.0: a ``[dependencies]`` section
in the manifest, a GitHub-hosted (or local-directory) ``index.json``
registry, and vendoring into ``vendor/<name>/<version>/`` plus a
``Kabir.lock`` lockfile. No server is involved and ``publish``
never pushes anything anywhere -- it only builds the tarball and
stages the exact registry update (``stage_publish``) for the user
to push by hand. The index format is documented in
docs/REGISTRY.md.

The manifest (``Kabir.toml``) is read with a hand-rolled minimal
parser for the exact format :func:`new_project` writes -- ``tomllib``
is Python 3.11+ and Kabir supports Python 3.8+, so it cannot be used.

The compiler and the type checker are imported lazily, inside
:func:`build_project`, so this module stays importable while the
rest of the 2.0.0 pipeline is still being built. Nothing in this
module prints: the CLI owns all output.
"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from .errors import KabirError

__all__ = ["new_project", "build_project", "run_project",
           "parse_manifest", "read_manifest",
           "add_package", "publish_package", "stage_publish",
           "read_lockfile", "validate_index"]

# Project names: start with a letter, then letters, digits, "-" or "_".
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

MANIFEST_FILENAME = "Kabir.toml"
MAIN_SOURCE = Path("src") / "main.kbr"
TARGET_DIR = "target"

DEFAULT_VERSION = "0.1.0"
DEFAULT_AUTHOR = "Afrain Kabir Rafid"

# --- Package registry (3.0.0 Wave 2, extended in 5.0.0 Wave 2) ---------
# Registry index format (index.json in the registry) -- see
# docs/REGISTRY.md for the full schema. Short version::
#
#   {"packages": {"name": {"1.0.0": {"url": ..., "sha256": ...,
#                                    "description": ...,
#                                    "dependencies": {...}}}}}
#
# ``url`` is either an absolute http(s) URL, or a path relative to
# the registry (a directory inside a local registry, or a path under
# the URL base of a remote registry). ``sha256`` is the hex digest
# of the package tarball; when present it is verified before
# anything is installed, and when absent the digest of the
# downloaded file is recorded in the lockfile instead.
# ``description`` is free text from the package's Kabir.toml.
# ``dependencies`` maps a package name to an exact version and is
# installed transitively by ``add_package``.
REGISTRY_ENV_VAR = "KABIR_REGISTRY"
# The future registry repo. It does not exist yet: against this
# default, add_package() refuses with a clear error telling the user
# to set KABIR_REGISTRY to a local registry directory.
DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/afrainkabirrafid/"
    "kabir-registry/main"
)
REGISTRY_INDEX_FILENAME = "index.json"
LOCK_FILENAME = "Kabir.lock"
VENDOR_DIR = "vendor"

# Exact versions only: "1.0.0". No ranges ("^1.0.0", ">=1.0.0", "*").
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Keys allowed in the [package] section of Kabir.toml.
_PACKAGE_KEYS = ("name", "version", "authors", "description")

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SECTION_RE = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_-]*)\]$")
_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$")


def _manifest_error(message, filename="Kabir.toml", line=None):
    return KabirError(message, filename=filename, line=line)


def _strip_comment(line):
    """Remove a ``#`` comment that appears outside a quoted string."""
    in_str = False
    escaped = False
    for i, ch in enumerate(line):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            return line[:i]
    return line


def _parse_value(raw, filename, lineno):
    """Parse one manifest value: a quoted string or a list of strings."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError:
            raise _manifest_error(
                "Invalid string value %r -- use double quotes, e.g. "
                'name = "my_app"' % raw, filename, lineno)
        if not isinstance(value, str):
            raise _manifest_error(
                "Invalid value %r -- expected a quoted string" % raw,
                filename, lineno)
        return value
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except ValueError:
            raise _manifest_error(
                "Invalid list value %r -- use double-quoted strings, "
                'e.g. authors = ["Afrain Kabir Rafid"]' % raw,
                filename, lineno)
        if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value):
            raise _manifest_error(
                "Invalid list value %r -- expected a list of quoted "
                "strings" % raw, filename, lineno)
        return value
    raise _manifest_error(
        "Invalid value %r -- expected a quoted string or a list of "
        "quoted strings" % raw, filename, lineno)


def parse_manifest(text, filename="Kabir.toml"):
    """Parse the text of a ``Kabir.toml`` manifest.

    Returns a dict with ``name`` (str), ``version`` (str),
    ``authors`` (list of str), ``description`` (str) and
    ``dependencies`` (dict mapping a package name to an exact
    version string, e.g. ``{"greet": "1.0.0"}``). ``version``
    defaults to ``"0.1.0"``, ``authors`` to ``[]``, ``description``
    to ``""`` and ``dependencies`` to ``{}`` when they are absent.
    Dependency versions must be exact (``"1.0.0"`` -- no ranges).
    Raises :class:`KabirError` on any malformed manifest.
    """
    section = None
    seen_sections = set()
    package = {}
    dependencies = {}
    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
            if section in seen_sections:
                raise _manifest_error(
                    "Duplicate section [%s] in %s" % (section, filename),
                    filename, lineno)
            seen_sections.add(section)
            if section not in ("package", "dependencies"):
                raise _manifest_error(
                    'Unknown section "[%s]" in %s -- only [package] and '
                    "[dependencies] are supported" % (section, filename),
                    filename, lineno)
            continue
        match = _KEY_VALUE_RE.match(line)
        if match is None:
            raise _manifest_error(
                "Cannot understand line %d in %s: %r -- expected "
                "'key = value' inside [%s]"
                % (lineno, filename, raw_line.strip(),
                   section if section is not None else "package"),
                filename, lineno)
        if section is None:
            raise _manifest_error(
                "Key %r appears before any section in %s -- put it "
                "under [package]" % (match.group(1), filename),
                filename, lineno)
        key = match.group(1)
        value = _parse_value(match.group(2), filename, lineno)
        if section == "package":
            if key not in _PACKAGE_KEYS:
                raise _manifest_error(
                    'Unknown key "%s" in [package] -- only name, version '
                    "and authors are supported" % key, filename, lineno)
            if key in package:
                raise _manifest_error(
                    'Duplicate key "%s" in [package]' % key,
                    filename, lineno)
            package[key] = value
        else:  # [dependencies]
            if not NAME_RE.match(key):
                raise _manifest_error(
                    'Invalid dependency name %r in [dependencies] of %s '
                    "-- names must start with a letter and contain only "
                    'letters, digits, "-" and "_"'
                    % (key, filename), filename, lineno)
            if key in dependencies:
                raise _manifest_error(
                    'Duplicate dependency "%s" in [dependencies]'
                    % key, filename, lineno)
            if not isinstance(value, str) or not _VERSION_RE.match(value):
                raise _manifest_error(
                    'Invalid version %r for dependency "%s" in %s -- '
                    'versions must be exact, e.g. "1.0.0" (no ranges '
                    'like ">=1.0.0" or "^1.0.0")'
                    % (value, key, filename), filename, lineno)
            dependencies[key] = value
    if "package" not in seen_sections:
        raise _manifest_error(
            "Missing [package] section in %s -- run 'kabir new' to "
            "create a project" % filename, filename)
    if "name" not in package:
        raise _manifest_error(
            'Missing "name" in [package] of %s' % filename, filename)
    for key in ("name", "version", "description"):
        if key in package and not isinstance(package[key], str):
            raise _manifest_error(
                'Key "%s" in [package] of %s must be a quoted string'
                % (key, filename), filename)
    if "authors" in package and not isinstance(package["authors"], list):
        raise _manifest_error(
            'Key "authors" in [package] of %s must be a list of quoted '
            "strings" % filename, filename)
    name = package["name"]
    if not name or not NAME_RE.match(name):
        raise _manifest_error(
            'Invalid project name %r in %s -- names must start with a '
            'letter and contain only letters, digits, "-" and "_"'
            % (name, filename), filename)
    return {
        "name": name,
        "version": package.get("version", DEFAULT_VERSION),
        "authors": package.get("authors", []),
        "description": package.get("description", ""),
        "dependencies": dependencies,
    }


def read_manifest(path):
    """Read and parse the manifest at *path* (a Kabir.toml file).

    Raises :class:`KabirError` when the file is missing or malformed.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KabirError(
            "No %s found -- run 'kabir new <name>' to create a project"
            % MANIFEST_FILENAME, filename=str(path))
    except OSError as exc:
        raise KabirError("Cannot read %s -- %s" % (path, exc),
                         filename=str(path))
    return parse_manifest(text, filename=str(path))


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise KabirError(
            "Invalid project name %r -- names must start with a letter "
            'and contain only letters, digits, "-" and "_"'
            % (name,), filename="<project>")


def new_project(name, cwd="."):
    """Create a new Kabir project named *name* inside *cwd*.

    Creates ``<cwd>/<name>/`` with ``Kabir.toml``, ``src/main.kbr``
    and ``.gitignore``. Raises :class:`KabirError` for an invalid
    name or when the directory already exists. Returns the created
    project directory as a :class:`pathlib.Path`.
    """
    _check_name(name)
    project_dir = Path(cwd) / name
    if project_dir.exists():
        raise KabirError('Project "%s" already exists' % name,
                         filename=str(project_dir))
    src_dir = project_dir / "src"
    try:
        src_dir.mkdir(parents=True)
    except OSError as exc:
        raise KabirError("Cannot create project %r -- %s" % (name, exc),
                         filename=str(project_dir))
    manifest = (
        "[package]\n"
        'name = "%s"\n'
        'version = "%s"\n'
        'authors = ["%s"]\n'
        % (name, DEFAULT_VERSION, DEFAULT_AUTHOR)
    )
    main_source = (
        "func main() {\n"
        '    print("Hello from %s!")\n'
        "}\n"
        "\n"
        "main()\n"
        % name
    )
    (project_dir / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")
    (src_dir / "main.kbr").write_text(main_source, encoding="utf-8")
    (project_dir / ".gitignore").write_text("target/\nvendor/\n",
                                            encoding="utf-8")
    return project_dir


def run_project(path):
    """Resolve ``src/main.kbr`` for the project at *path*.

    Returns the entry-point file as a :class:`pathlib.Path`.
    Raises :class:`KabirError` when the project directory or its
    ``src/main.kbr`` does not exist.
    """
    project_dir = Path(path)
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    main_path = project_dir / MAIN_SOURCE
    if not main_path.is_file():
        raise KabirError(
            "No src/main.kbr found in %s -- run 'kabir new <name>' to "
            "create a project" % project_dir, filename=str(main_path))
    return main_path


def build_project(path="."):
    """Build the project at *path* to bytecode.

    Reads ``Kabir.toml``, compiles ``src/main.kbr`` (parse -> check
    -> compile, with imports resolved relative to ``src/``) and
    writes ``target/<name>.kbc``. Returns the output file as a
    :class:`pathlib.Path`. Prints nothing.

    The checker and compiler are imported lazily, at call time, so
    this module stays importable while they are still being built.
    """
    project_dir = Path(path).resolve()
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    manifest = read_manifest(project_dir / MANIFEST_FILENAME)
    main_path = run_project(project_dir)
    try:
        source = main_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KabirError("Cannot read %s -- %s" % (main_path, exc),
                         filename=str(main_path))

    # Lazy imports: kabir/checker.py and kabir/compile.py are the
    # compiler pipeline; the ImportError branches below are a guard
    # for a broken install, not an expected state.
    try:
        from .checker import check_program
    except ImportError:
        raise KabirError(
            "Cannot build: the type checker (kabir/checker.py) is not "
            "available -- the install may be broken",
            filename=str(main_path))
    try:
        from .compile import compile_program, save_bytecode
    except ImportError:
        raise KabirError(
            "Cannot build: the bytecode compiler (kabir/compile.py) "
            "is not available -- the install may be broken",
            filename=str(main_path))

    main_filename = str(main_path.resolve())
    lines = source.split("\n")
    from .parser import parse
    program = parse(source, main_filename)
    check_program(program, filename=main_filename, lines=lines)
    code = compile_program(program, main_filename, lines)

    target_dir = project_dir / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / ("%s.kbc" % manifest["name"])
    save_bytecode(code, str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# Package registry (3.0.0 Wave 2, extended in 5.0.0 Wave 2)
#
# The registry is a static GitHub repo (or a local directory with
# the same layout): an ``index.json`` at the root plus one tarball
# per package version, normally under ``packages/``. The full
# schema and the hosting model are documented in docs/REGISTRY.md.
# Short version of the index format::
#
#     {"packages": {"name": {"1.0.0": {"url": ...,
#                                      "sha256": ...,
#                                      "description": ...,
#                                      "dependencies": {...}}}}}
#
# ``url`` is an absolute http(s) URL, or a path relative to the
# registry: a file inside a local registry directory, or a path
# under the URL base of a remote registry. ``sha256`` is the hex
# digest of the package tarball; when present it is verified before
# anything is installed, and when absent the digest of the
# downloaded file is recorded in the lockfile instead.
# ``description`` is free text from the package's Kabir.toml.
# ``dependencies`` maps a package name to an exact version string
# and is installed transitively by ``add_package``.
#
# The registry source is the ``KABIR_REGISTRY`` environment variable
# (a local directory, used by tests, or a URL base). The default is
# the GitHub raw URL of the future registry repo, which does not
# exist yet -- ``add_package`` refuses it with a clear error.
# ``stage_publish`` stages against an empty index when no registry
# is configured yet (a first publish). There is no server and
# nothing here pushes anywhere.
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_index(index):
    """Validate a parsed registry ``index.json``.

    The index must be ``{"packages": {name: {version: entry}}}``
    where each entry is a dict with a non-empty ``"url"`` string,
    an optional ``"sha256"`` (64 lowercase hex chars), an optional
    ``"description"`` string and optional ``"dependencies"``
    mapping a package name to an exact version string. Unknown
    extra keys are ignored, so the format can grow without
    breaking old readers. Returns the index unchanged. Raises
    :class:`KabirError` on any malformed part, naming the
    offending package/version.
    """
    if not isinstance(index, dict) or not isinstance(
            index.get("packages"), dict):
        raise KabirError(
            "Registry index is malformed -- expected "
            '{"packages": {"name": {"1.0.0": {"url": ..., '
            '"sha256": ...}}}}', filename="<registry>")
    for name, versions in index["packages"].items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise KabirError(
                'Registry index has an invalid package name %r'
                % (name,), filename="<registry>")
        if not isinstance(versions, dict):
            raise KabirError(
                'Registry index entry for package "%s" is malformed '
                "-- expected a mapping of version to entry" % name,
                filename="<registry>")
        for version, entry in versions.items():
            where = 'package "%s" version %s' % (name, version)
            if not isinstance(version, str) or not _VERSION_RE.match(
                    version):
                raise KabirError(
                    'Registry index has an invalid version %r for '
                    'package "%s" -- versions must be exact, e.g. '
                    '"1.0.0"' % (version, name), filename="<registry>")
            if not isinstance(entry, dict):
                raise KabirError(
                    "Registry index entry for %s is malformed -- "
                    'expected {"url": ..., "sha256": ...}' % where,
                    filename="<registry>")
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                raise KabirError(
                    'Registry index entry for %s has no "url"'
                    % where, filename="<registry>")
            sha256 = entry.get("sha256")
            if sha256 is not None and (
                    not isinstance(sha256, str)
                    or not _SHA256_RE.match(sha256)):
                raise KabirError(
                    'Registry index entry for %s has an invalid '
                    '"sha256" -- expected 64 lowercase hex chars'
                    % where, filename="<registry>")
            description = entry.get("description")
            if description is not None and not isinstance(
                    description, str):
                raise KabirError(
                    'Registry index entry for %s has a non-string '
                    '"description"' % where, filename="<registry>")
            dependencies = entry.get("dependencies")
            if dependencies is not None:
                if not isinstance(dependencies, dict):
                    raise KabirError(
                        'Registry index entry for %s has a '
                        'non-mapping "dependencies"' % where,
                        filename="<registry>")
                for dep_name, dep_version in dependencies.items():
                    if not isinstance(dep_name, str) or not NAME_RE.match(
                            dep_name):
                        raise KabirError(
                            'Registry index entry for %s has an invalid '
                            'dependency name %r' % (where, dep_name),
                            filename="<registry>")
                    if not isinstance(dep_version, str) or not (
                            _VERSION_RE.match(dep_version)):
                        raise KabirError(
                            'Registry index entry for %s has an invalid '
                            'version %r for dependency "%s" -- versions '
                            'must be exact, e.g. "1.0.0"'
                            % (where, dep_version, dep_name),
                            filename="<registry>")
    return index


def _latest_version(versions):
    """Return the highest exact version in *versions* (a dict).

    Versions are compared as (major, minor, patch) number tuples.
    Raises :class:`KabirError` when the dict is empty.
    """
    if not versions:
        return None
    return max(versions,
               key=lambda v: tuple(int(part) for part in v.split(".")))


def _resolve_registry(registry=None):
    """Resolve the registry source.

    Returns ``("local", Path)`` for a local registry directory,
    ``("url", base_url)`` for a URL base, or ``("default", url)``
    when no registry was given anywhere -- the default registry
    repo does not exist yet, so callers must refuse it with a
    clear error instead of fetching from it.
    """
    raw = registry
    if raw is None:
        raw = os.environ.get(REGISTRY_ENV_VAR, "") or None
    if raw is None:
        return ("default", DEFAULT_REGISTRY_URL)
    if isinstance(raw, Path):
        raw = str(raw)
    if isinstance(raw, str) and raw.startswith(("http://", "https://")):
        return ("url", raw.rstrip("/"))
    path = Path(raw)
    if path.is_dir():
        return ("local", path)
    raise KabirError(
        'Registry "%s" does not exist -- set %s to a local registry '
        "directory or a registry URL base" % (raw, REGISTRY_ENV_VAR),
        filename=REGISTRY_ENV_VAR)


def _http_get_bytes(url, what):
    """Fetch *url* with urllib; any failure becomes a KabirError."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise KabirError(
            "Cannot fetch %s from %s -- HTTP %s" % (what, url, exc.code),
            filename=url)
    except urllib.error.URLError as exc:
        raise KabirError(
            "Cannot fetch %s from %s -- %s" % (what, url, exc.reason),
            filename=url)
    except OSError as exc:
        raise KabirError(
            "Cannot fetch %s from %s -- %s" % (what, url, exc),
            filename=url)


def _read_index(source, missing_ok=False):
    """Load and validate the registry ``index.json`` for *source*.

    With ``missing_ok=True`` a missing index (no ``index.json``
    in a local registry directory, or HTTP 404 on a URL base)
    returns an empty index ``{"packages": {}}`` instead of
    raising -- used by :func:`stage_publish` for a first publish
    into a fresh registry. Any other failure (unreadable file,
    invalid JSON, malformed index) still raises.
    """
    kind, base = source
    if kind == "local":
        index_path = base / REGISTRY_INDEX_FILENAME
        try:
            raw = index_path.read_bytes()
        except FileNotFoundError:
            if missing_ok:
                return {"packages": {}}
            raise KabirError(
                "Registry index not found at %s -- expected an "
                "index.json in the registry directory" % index_path,
                filename=str(index_path))
        except OSError as exc:
            raise KabirError(
                "Cannot read registry index %s -- %s"
                % (index_path, exc), filename=str(index_path))
    else:  # ("url", base)
        if missing_ok:
            try:
                raw = _http_get_bytes(
                    "%s/%s" % (base, REGISTRY_INDEX_FILENAME),
                    "registry index")
            except KabirError as exc:
                if "HTTP 404" in str(exc):
                    return {"packages": {}}
                raise
        else:
            raw = _http_get_bytes("%s/%s" % (base, REGISTRY_INDEX_FILENAME),
                                  "registry index")
    try:
        index = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise KabirError(
            "Registry index is not valid JSON -- expected "
            '{"packages": {...}}', filename="<registry>")
    return validate_index(index)


def _fetch_package_bytes(entry, source):
    """Fetch the tarball bytes for one registry *entry*."""
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise KabirError(
            'Registry entry has no "url" -- the package cannot be '
            "downloaded", filename="<registry>")
    if url.startswith(("http://", "https://")):
        return _http_get_bytes(url, "package")
    kind, base = source
    if kind == "local":
        path = Path(url)
        if not path.is_absolute():
            path = base / url
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise KabirError(
                "Package file not found in registry: %s" % path,
                filename=str(path))
        except OSError as exc:
            raise KabirError(
                "Cannot read package file %s -- %s" % (path, exc),
                filename=str(path))
    return _http_get_bytes("%s/%s" % (base, url.lstrip("/")), "package")


def _verify_sha256(data, expected, name, version):
    """Check *data* against the registry's sha256; return the digest."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected.lower():
        raise KabirError(
            'sha256 mismatch for package "%s" %s -- the registry says '
            "%s but the downloaded file hashes to %s; the package "
            "may have been tampered with, so it was not installed"
            % (name, version, expected, digest), filename="<registry>")
    return digest


def _extract_tarball(data, dest_dir, name, version):
    """Extract a package tarball into *dest_dir* (created fresh).

    Member paths are checked first: absolute paths and ``..``
    components are refused, so a hostile tarball cannot write
    outside the vendor directory.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError:
        raise KabirError(
            'Package "%s" %s is not a valid tarball -- expected a '
            ".tar.gz package file" % (name, version),
            filename="<registry>")
    with tar:
        for member in tar.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise KabirError(
                    'Package "%s" %s contains an unsafe path %r -- '
                    "refusing to extract" % (name, version, member.name),
                    filename="<registry>")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            tar.extractall(path=dest_dir, filter="data")
        except TypeError:  # Python < 3.11.4: no filter argument
            tar.extractall(path=dest_dir)
    return dest_dir


def _set_dependency(manifest_path, name, version):
    """Record ``name = "version"`` in the [dependencies] section of
    the manifest at *manifest_path*, preserving the rest of the file
    (comments, ordering, other sections) untouched.
    """
    lines = manifest_path.read_text(encoding="utf-8").splitlines(keepends=True)
    section = None
    dep_header = None
    existing = None
    for index, raw_line in enumerate(lines):
        line = _strip_comment(raw_line).strip()
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
            if section == "dependencies" and dep_header is None:
                dep_header = index
            continue
        if section == "dependencies":
            match = _KEY_VALUE_RE.match(line)
            if match is not None and match.group(1) == name:
                existing = index
                break
    new_line = '%s = "%s"\n' % (name, version)
    if existing is not None:
        lines[existing] = new_line
    elif dep_header is not None:
        lines.insert(dep_header + 1, new_line)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip() != "":
            lines.append("\n")
        lines.append("[dependencies]\n")
        lines.append(new_line)
    manifest_path.write_text("".join(lines), encoding="utf-8")


def read_lockfile(project_dir):
    """Read ``Kabir.lock`` in *project_dir*.

    Returns ``{name: {"version": ..., "sha256": ...}}``; ``{}``
    when there is no lockfile yet. Raises :class:`KabirError` on a
    malformed lockfile. Lockfile format (JSON)::

        {"packages": {"greet": {"version": "1.0.0",
                                "sha256": "<hex>"}}}
    """
    path = Path(project_dir) / LOCK_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise KabirError("Cannot read %s -- %s" % (path, exc),
                         filename=str(path))
    if not isinstance(data, dict) or not isinstance(
            data.get("packages"), dict):
        raise KabirError(
            'Malformed %s -- expected {"packages": {...}}'
            % LOCK_FILENAME, filename=str(path))
    return data["packages"]


def _write_lockfile(project_dir, packages):
    """Write ``Kabir.lock`` in *project_dir* from *packages*."""
    path = Path(project_dir) / LOCK_FILENAME
    payload = {"packages": {name: packages[name]
                            for name in sorted(packages)}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _install_from_index(name, version, project_dir, index, source,
                        seen_versions, installed):
    """Install one package (and its transitive dependencies) from
    an already-loaded *index*.

    *seen_versions* maps an already-requested package name to the
    version being installed -- a second request for the same name
    at a different version is a conflict and raises; the same
    version twice (a diamond) installs once. *installed* is the
    running lockfile map ``{name: {"version", "sha256"}}`` and is
    extended in place. Returns the sha256 digest of this package.
    """
    if name in seen_versions:
        if seen_versions[name] != version:
            raise KabirError(
                'Version conflict for package "%s" -- %s is required '
                "but %s is already installed; Kabir resolves exact "
                "versions only, so the conflicting packages must "
                "agree on one version"
                % (name, version, seen_versions[name]),
                filename="<registry>")
        return installed[name]["sha256"]
    seen_versions[name] = version
    versions = index["packages"].get(name)
    if not isinstance(versions, dict):
        raise KabirError(
            'Package "%s" not found in the registry' % name,
            filename="<registry>")
    entry = versions.get(version)
    if not isinstance(entry, dict):
        available = ", ".join(sorted(versions)) or "(none)"
        raise KabirError(
            'Package "%s" has no version %s in the registry -- '
            "available versions: %s" % (name, version, available),
            filename="<registry>")
    data = _fetch_package_bytes(entry, source)
    expected = entry.get("sha256")
    if isinstance(expected, str) and expected:
        digest = _verify_sha256(data, expected, name, version)
    else:
        digest = hashlib.sha256(data).hexdigest()
    vendor_dir = project_dir / VENDOR_DIR / name / version
    if vendor_dir.exists():
        shutil.rmtree(vendor_dir)
    _extract_tarball(data, vendor_dir, name, version)
    installed[name] = {"version": version, "sha256": digest}
    for dep_name, dep_version in sorted(
            (entry.get("dependencies") or {}).items()):
        _install_from_index(dep_name, dep_version, project_dir, index,
                            source, seen_versions, installed)
    return digest


def add_package(name, version, project_dir=".", registry=None):
    """Add an exact-version dependency to the project at *project_dir*.

    Resolves ``name``/``version`` against the registry (see
    :func:`_resolve_registry`); ``version`` may also be ``"latest"``,
    which picks the highest version in the index. Verifies the
    package sha256 when the registry entry has one, extracts the
    tarball into ``vendor/<name>/<version>/``, records
    ``name = "version"`` in the manifest's ``[dependencies]``
    section, and writes ``Kabir.lock``. Dependencies listed in
    the registry entry are installed transitively (vendored and
    locked, but not added to the manifest -- only the package
    asked for is a direct dependency). Re-adding the same version
    re-vendors it cleanly. Returns ``{"name", "version", "sha256",
    "vendor_dir", "lockfile"}``. Prints nothing. Raises
    :class:`KabirError` for an unknown package, an unknown
    version, a sha256 mismatch, a version conflict between
    transitive dependencies, a missing registry/index/package
    file, or the not-yet-live default registry.
    """
    _check_name(name)
    if not isinstance(version, str) or not (
            version == "latest" or _VERSION_RE.match(version)):
        raise KabirError(
            'Invalid version %r for package "%s" -- versions must be '
            'exact, e.g. "1.0.0" (or "latest"), no ranges like '
            '">=1.0.0" or "^1.0.0"' % (version, name),
            filename="<registry>")
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    manifest_path = project_dir / MANIFEST_FILENAME
    read_manifest(manifest_path)  # raises when missing or malformed
    source = _resolve_registry(registry)
    if source[0] == "default":
        raise KabirError(
            "registry not set up yet — set KABIR_REGISTRY to a local "
            "registry dir (or a registry URL base). The default "
            "registry (%s) does not exist yet, so no packages can "
            "be fetched from it." % DEFAULT_REGISTRY_URL,
            filename=REGISTRY_ENV_VAR)
    index = _read_index(source)
    if version == "latest":
        versions = index["packages"].get(name)
        if not isinstance(versions, dict) or not versions:
            raise KabirError(
                'Package "%s" not found in the registry' % name,
                filename="<registry>")
        version = _latest_version(versions)
    installed = {}
    digest = _install_from_index(name, version, project_dir, index,
                                 source, {}, installed)
    _set_dependency(manifest_path, name, version)
    locked = read_lockfile(project_dir)
    locked.update(installed)
    lock_path = _write_lockfile(project_dir, locked)
    vendor_dir = project_dir / VENDOR_DIR / name / version
    return {
        "name": name,
        "version": version,
        "sha256": digest,
        "vendor_dir": str(vendor_dir),
        "lockfile": str(lock_path),
    }


def publish_package(project_dir="."):
    """Package the project at *project_dir* for the registry.

    Builds a deterministic ``target/<name>-<version>.tar.gz``
    containing ``Kabir.toml`` and everything under ``src/``, and
    returns ``{"name", "version", "description", "dependencies",
    "tarball", "sha256", "files", "registry_entry", "steps"}``.
    ``registry_entry`` is the exact JSON fragment to merge into
    the registry's ``index.json``::

        {"packages": {"<name>": {"<version>":
            {"url": "packages/<name>-<version>.tar.gz",
             "sha256": "<hex>",
             "description": "...",
             "dependencies": {"other": "1.0.0"}}}}}

    ``description`` and ``dependencies`` appear in the entry only
    when the manifest sets them (``description`` in ``[package]``,
    ``dependencies`` in ``[dependencies]``), so entries stay
    minimal and old readers keep working. ``steps`` lists how to
    submit the tarball + entry; use :func:`stage_publish` to get
    the staged files and exact push commands instead. This
    function does NOT push anything anywhere. Prints nothing.
    Raises :class:`KabirError` when the manifest or
    ``src/main.kbr`` is missing, or when the version is not an
    exact ``"1.2.3"`` version (a registry version must be exact).
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    manifest = read_manifest(project_dir / MANIFEST_FILENAME)
    run_project(project_dir)  # raises when src/main.kbr is missing
    name = manifest["name"]
    version = manifest["version"]
    if not _VERSION_RE.match(version):
        raise KabirError(
            'Cannot publish package "%s" -- version %r is not an '
            'exact "1.2.3" version; fix version in Kabir.toml'
            % (name, version), filename=str(project_dir / MANIFEST_FILENAME))
    description = manifest.get("description", "")
    dependencies = manifest.get("dependencies", {})
    target_dir = project_dir / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    tarball_name = "%s-%s.tar.gz" % (name, version)
    tarball_path = target_dir / tarball_name
    entries = [(MANIFEST_FILENAME, project_dir / MANIFEST_FILENAME)]
    src_dir = project_dir / "src"
    for path in sorted(src_dir.rglob("*")):
        if path.is_file():
            entries.append((str(path.relative_to(project_dir)), path))
    files = []
    raw = open(tarball_path, "wb")
    try:
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
        try:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for arcname, path in entries:
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with open(path, "rb") as handle:
                        tar.addfile(info, handle)
                    files.append(arcname)
        finally:
            gz.close()
    finally:
        raw.close()
    digest = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    url = "packages/%s" % tarball_name
    entry = {"url": url, "sha256": digest}
    if description:
        entry["description"] = description
    if dependencies:
        entry["dependencies"] = dict(dependencies)
    registry_entry = {
        "packages": {
            name: {
                version: entry,
            },
        },
    }
    steps = [
        "1. Copy %s into the registry repo under packages/ (as %s)."
        % (tarball_name, url),
        "2. Merge this entry into the registry repo's index.json "
        'under "packages".',
        "3. Commit and push the registry repo.",
        "4. Others can then run: kabir add %s@%s" % (name, version),
    ]
    return {
        "name": name,
        "version": version,
        "description": description,
        "dependencies": dict(dependencies),
        "tarball": str(tarball_path),
        "sha256": digest,
        "files": files,
        "registry_entry": registry_entry,
        "steps": steps,
    }

def _registry_repo_url(source):
    """Best-effort GitHub repo URL for a registry *source*.

    A ``raw.githubusercontent.com/<owner>/<repo>/<branch>`` base
    maps to ``https://github.com/<owner>/<repo>``; anything else
    returns ``None`` (the push commands then use a placeholder
    for the clone path instead of guessing).
    """
    kind, base = source
    if kind != "url":
        return None
    match = re.match(r"https://raw\.githubusercontent\.com/"
                     r"([^/]+)/([^/]+)/([^/]+)/?$", base)
    if match is None:
        return None
    return "https://github.com/%s/%s" % (match.group(1), match.group(2))


def _push_commands(stage_dir, tarball_name, name, version, source,
                   registry_path=None):
    """Exact shell commands that publish the staged files.

    Returns a list of single-line commands. When the registry is
    a local directory the commands copy the staged files into it
    and commit there; when it is a URL base (or the not-yet-live
    default) the commands clone the index repo first. Nothing is
    executed here -- the user runs these by hand.
    """
    staged_index = str(stage_dir / REGISTRY_INDEX_FILENAME)
    staged_tarball = str(stage_dir / "packages" / tarball_name)
    if registry_path is not None:
        registry_dir = str(registry_path)
        return [
            "cp %s %s" % (staged_index,
                          os.path.join(registry_dir, "index.json")),
            "mkdir -p %s && cp %s %s"
            % (os.path.join(registry_dir, "packages"), staged_tarball,
               os.path.join(registry_dir, "packages", tarball_name)),
            'cd %s && git add index.json packages/%s && git commit '
            '-m "Add %s %s to the registry" && git push'
            % (registry_dir, tarball_name, name, version),
        ]
    repo_url = _registry_repo_url(source)
    if repo_url is None:
        repo_url = _registry_repo_url(("url", DEFAULT_REGISTRY_URL))
    repo_dir = repo_url.rsplit("/", 1)[-1]
    if repo_dir.endswith(".git"):
        repo_dir = repo_dir[:-4]
    return [
        "git clone %s.git" % repo_url,
        "cp %s %s" % (staged_index,
                      os.path.join(repo_dir, "index.json")),
        "mkdir -p %s && cp %s %s"
        % (os.path.join(repo_dir, "packages"), staged_tarball,
           os.path.join(repo_dir, "packages", tarball_name)),
        'cd %s && git add index.json packages/%s && git commit '
        '-m "Add %s %s to the registry" && git push'
        % (repo_dir, tarball_name, name, version),
    ]


def stage_publish(project_dir=".", registry=None, stage_dir=None):
    """Stage a publish: build the package and prepare the exact
    registry update, without pushing anything.

    Builds the tarball with :func:`publish_package`, merges its
    entry into the registry index (the index at *registry* /
    ``KABIR_REGISTRY`` when one is configured, otherwise a fresh
    empty index), and writes the updated ``index.json`` plus the
    tarball into a staging directory (default
    ``<project>/target/registry-stage/``). The staged index is
    re-read with :func:`_read_index` before returning, so a
    broken staged index can never be handed back silently.

    Returns ``{"name", "version", "description", "dependencies",
    "tarball", "sha256", "files", "registry_entry", "stage_dir",
    "staged_index", "staged_tarball", "index", "commands",
    "steps"}``. ``index`` is the full merged index dict;
    ``commands`` are the exact shell lines that publish the
    staged files (copy into the registry repo, commit, push);
    ``steps`` is the same plan in human-readable form. Prints
    nothing. Raises :class:`KabirError` when the package cannot
    be built, or when the index already contains this
    name@version (bump the version and publish again instead of
    silently overwriting a released version).
    """
    project_dir = Path(project_dir)
    published = publish_package(project_dir)
    name = published["name"]
    version = published["version"]
    tarball_name = "%s-%s.tar.gz" % (name, version)
    source = _resolve_registry(registry)
    if source[0] == "default":
        base_index = {"packages": {}}
        registry_path = None
    else:
        base_index = _read_index(source, missing_ok=True)
        registry_path = source[1] if source[0] == "local" else None
    existing = base_index["packages"].get(name, {})
    if version in existing:
        raise KabirError(
            'Package "%s" version %s is already in the registry '
            "index -- bump the version in Kabir.toml and publish "
            "again; released versions are never overwritten"
            % (name, version), filename="<registry>")
    merged = json.loads(json.dumps(base_index))
    merged["packages"].setdefault(name, {})[version] = (
        published["registry_entry"]["packages"][name][version])
    validate_index(merged)
    if stage_dir is None:
        stage_dir = project_dir / TARGET_DIR / "registry-stage"
    stage_dir = Path(stage_dir)
    if stage_dir.is_dir():
        shutil.rmtree(stage_dir)
    elif stage_dir.exists():
        stage_dir.unlink()
    (stage_dir / "packages").mkdir(parents=True)
    staged_index = stage_dir / REGISTRY_INDEX_FILENAME
    staged_index.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    staged_tarball = stage_dir / "packages" / tarball_name
    shutil.copyfile(published["tarball"], staged_tarball)
    # Verify the staged files the same way `add` reads them.
    staged = _read_index(("local", stage_dir))
    staged_entry = staged["packages"][name][version]
    if staged_entry["sha256"] != published["sha256"]:
        raise KabirError(
            "Staged index for %s %s does not match the built "
            "tarball -- refusing to hand back a broken stage"
            % (name, version), filename=str(staged_index))
    commands = _push_commands(stage_dir, tarball_name, name, version,
                              source, registry_path)
    if registry_path is not None:
        steps = [
            "1. Staged files are in %s (index.json + packages/%s)."
            % (stage_dir, tarball_name),
            "2. Copy them into the registry directory %s."
            % registry_path,
            "3. Commit and push the registry repo (commands below).",
            "4. Others can then run: kabir add %s@%s"
            % (name, version),
        ]
    else:
        steps = [
            "1. Staged files are in %s (index.json + packages/%s)."
            % (stage_dir, tarball_name),
            "2. Copy them into the registry repo (clone it first "
            "if needed -- commands below).",
            "3. Commit and push the registry repo.",
            "4. Others can then run: kabir add %s@%s"
            % (name, version),
        ]
    return {
        "name": name,
        "version": version,
        "description": published["description"],
        "dependencies": published["dependencies"],
        "tarball": published["tarball"],
        "sha256": published["sha256"],
        "files": published["files"],
        "registry_entry": published["registry_entry"],
        "stage_dir": str(stage_dir),
        "staged_index": str(staged_index),
        "staged_tarball": str(staged_tarball),
        "index": merged,
        "commands": commands,
        "steps": steps,
    }
