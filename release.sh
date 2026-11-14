#!/usr/bin/env bash
# release.sh — one-command Kabir release.
#
#   ./release.sh <version> [--notes "short release notes"] [--dry-run]
#                        [--skip-tests] [--skip-pypi] [--skip-push]
#
# What it does, in order:
#   1. Validates the version (X.Y.Z, greater than the current one, and not
#      already on PyPI).
#   2. Requires a CHANGELOG.md entry for the version (add one yourself, or
#      pass --notes "text" and it is added with today's date).
#   3. Bumps the version in the 8 places that carry it literally:
#      pyproject.toml, kabir/__init__.py (both __version__ and the
#      docstring line), examples/hello.kbr, and the version assertions
#      in tests/test_cli_6_0.py, tests/test_examples.py,
#      tests/test_cli_5_0.py, tests/test_compiler_vm.py. (Docs prose that
#      mentions an old version is history, not a bump target — the script
#      never touches it.)
#   4. Runs the full test suite (python3 tests/run_tests.py).
#   5. Builds wheel + sdist and runs `twine check`.
#   6. Uploads both files to PyPI as project `kabir-lang`, then verifies
#      the PyPI JSON API shows the new version as latest.
#   7. Pushes a snapshot of the repo to GitHub (afrainkabirrafid/Kabir,
#      main) — this asks for approval on a card, same as a manual push —
#      then verifies the live head commit.
#   8. Fresh `pip install kabir-lang` in a clean venv and runs hello.kbr.
#
# PyPI token: the token value is NEVER stored by this script. Set
# TWINE_PASSWORD in the environment for the run, or leave it unset and
# the script prompts for it (input hidden). It lives only in the
# script's own shell and is unset after the upload.
#   TWINE_PASSWORD='pypi-...' ./release.sh 6.1.0
#
# The same token works for every future version — it is scoped to the
# project kabir-lang, not to a version. Its name (e.g.
# "kabir-lang-publish-5.0.0") is only a label.
#
# --dry-run changes nothing: it validates and prints the whole plan.
# --skip-tests / --skip-pypi / --skip-push run a partial release.
# The script never deletes anything from GitHub (push-files only
# creates/updates) and never touches .git state — the local clone's
# git HEAD is stale by design; the snapshot push is the source of truth.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

OWNER="afrainkabirrafid"
REPO="Kabir"
BRANCH="main"
PYPI_PROJECT="kabir-lang"
BUILD_VENV="${TMPDIR:-/tmp}/kabir-release-venv"
VERIFY_VENV="${TMPDIR:-/tmp}/kabir-release-verify"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

# ---------- arguments ----------
VERSION=""
NOTES=""
DRY_RUN=0
SKIP_TESTS=0
SKIP_PYPI=0
SKIP_PUSH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --notes)      NOTES="${2:-}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-pypi)  SKIP_PYPI=1; shift ;;
    --skip-push)  SKIP_PUSH=1; shift ;;
    -h|--help)    usage 0 ;;
    -*)           die "unknown flag: $1 (see --help)" ;;
    *)            [ -z "$VERSION" ] || die "only one version allowed"; VERSION="$1"; shift ;;
  esac
done
[ -n "$VERSION" ] || usage 1

[ -f pyproject.toml ] && [ -f kabir/__init__.py ] || die "run from the Kabir repo root"
[ -f CHANGELOG.md ] || die "CHANGELOG.md missing"

# ---------- 1. validate version ----------
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z (got '$VERSION')"
CURRENT="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)"
[ -n "$CURRENT" ] || die "cannot read current version from pyproject.toml"
if [ "$CURRENT" = "$VERSION" ]; then
  # Already bumped (e.g. a coordinator built the version first): allowed
  # only if the CHANGELOG entry exists (checked below) and PyPI does not
  # have it yet (checked below). The bump step is skipped.
  SKIP_BUMP=1
  info "tree is already at $VERSION — bump step will be skipped"
elif [ "$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -n1)" != "$VERSION" ]; then
  die "new version $VERSION must be greater than current $CURRENT"
else
  SKIP_BUMP=0
fi
info "release $CURRENT -> $VERSION"

if curl -fsSL "https://pypi.org/pypi/$PYPI_PROJECT/json" -o /tmp/kabir-pypi.json; then
  if python3 -c "import json,sys; sys.exit(0 if '$VERSION' in json.load(open('/tmp/kabir-pypi.json'))['releases'] else 1)"; then
    die "version $VERSION already exists on PyPI"
  fi
else
  die "cannot reach PyPI to check $VERSION (network?)"
fi

# ---------- 2. CHANGELOG ----------
TODAY="$(TZ=Asia/Dhaka date +%F)"
if ! grep -q "^## $VERSION" CHANGELOG.md; then
  if [ -n "$NOTES" ]; then
    info "adding CHANGELOG entry for $VERSION ($TODAY)"
    if [ "$DRY_RUN" = 0 ]; then
      { printf '## %s — %s\n\n%s\n\n' "$VERSION" "$TODAY" "$NOTES"; cat CHANGELOG.md; } > CHANGELOG.md.new
      mv CHANGELOG.md.new CHANGELOG.md
    else
      info "[dry-run] would prepend CHANGELOG entry: ## $VERSION — $TODAY"
    fi
  else
    die "CHANGELOG.md has no '## $VERSION' entry — add one, or pass --notes \"text\""
  fi
fi

# ---------- 3. bump the literal version spots (skipped if tree is already at the version) ----------
bump_in_file() {
  local file="$1" old="$2" new="$3"
  DRY_RUN="$DRY_RUN" python3 - "$file" "$old" "$new" <<'PYEOF'
import os, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
n = text.count(old)
if n == 0:
    print(f"ERROR: bump: {old!r} not found in {path} — update release.sh", file=sys.stderr)
    sys.exit(1)
if os.environ.get("DRY_RUN") == "1":
    print(f"[dry-run] {path}: would replace {n}x {old!r} -> {new!r}")
else:
    open(path, "w", encoding="utf-8").write(text.replace(old, new))
    print(f"{path}: replaced {n}x {old!r} -> {new!r}")
PYEOF
}
if [ "$SKIP_BUMP" = 0 ]; then
bump_in_file pyproject.toml            "version = \"$CURRENT\""       "version = \"$VERSION\""
bump_in_file kabir/__init__.py        "__version__ = \"$CURRENT\""   "__version__ = \"$VERSION\""
bump_in_file kabir/__init__.py        "Version $CURRENT."            "Version $VERSION."
bump_in_file examples/hello.kbr       "version $CURRENT."            "version $VERSION."
bump_in_file tests/test_cli_6_0.py    "kabir $CURRENT"               "kabir $VERSION"
bump_in_file tests/test_examples.py   "kabir $CURRENT"               "kabir $VERSION"
bump_in_file tests/test_examples.py   "Welcome to Kabir version $CURRENT." "Welcome to Kabir version $VERSION."
bump_in_file tests/test_cli_5_0.py    "Welcome to Kabir version $CURRENT." "Welcome to Kabir version $VERSION."
bump_in_file tests/test_compiler_vm.py "Welcome to Kabir version $CURRENT." "Welcome to Kabir version $VERSION."
fi

if [ "$DRY_RUN" = 1 ]; then
  info "[dry-run] plan:"
  info "[dry-run]   tests: python3 tests/run_tests.py"
  info "[dry-run]   build: python -m build + twine check dist/kabir_lang-$VERSION-*"
  info "[dry-run]   PyPI upload: twine upload dist/kabir_lang-$VERSION-py3-none-any.whl dist/kabir_lang-$VERSION.tar.gz"
  info "[dry-run]   GitHub push: snapshot of repo to $OWNER/$REPO ($BRANCH), message 'Kabir $VERSION'"
  info "[dry-run]   verify: PyPI JSON latest + GitHub head + clean-venv pip install"
  info "[dry-run] nothing was changed."
  exit 0
fi

# ---------- 4. tests ----------
if [ "$SKIP_TESTS" = 1 ]; then
  warn "skipping tests (--skip-tests) — release is unverified"
else
  info "running full test suite"
  python3 tests/run_tests.py
fi

# ---------- 5. build ----------
if [ ! -x "$BUILD_VENV/bin/python" ]; then
  info "creating build venv at $BUILD_VENV"
  python3 -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/python" -c "import build, twine" 2>/dev/null || \
  "$BUILD_VENV/bin/pip" install -q build twine
info "building wheel + sdist"
"$BUILD_VENV/bin/python" -m build
"$BUILD_VENV/bin/twine" check "dist/kabir_lang-$VERSION-py3-none-any.whl" "dist/kabir_lang-$VERSION.tar.gz"

# ---------- 6. PyPI upload ----------
if [ "$SKIP_PYPI" = 1 ]; then
  warn "skipping PyPI upload (--skip-pypi)"
else
  if [ -z "${TWINE_PASSWORD:-}" ]; then
    [ -t 0 ] || die "no TWINE_PASSWORD in environment and no terminal to prompt — set TWINE_PASSWORD and retry"
    printf 'PyPI token (input hidden, used once, never stored): '
    read -rs TWINE_PASSWORD; echo
    [ -n "$TWINE_PASSWORD" ] || die "no token given"
  fi
  info "uploading $VERSION to PyPI"
  TWINE_USERNAME=__token__ TWINE_PASSWORD="$TWINE_PASSWORD" \
    "$BUILD_VENV/bin/twine" upload --non-interactive \
    "dist/kabir_lang-$VERSION-py3-none-any.whl" "dist/kabir_lang-$VERSION.tar.gz"
  unset TWINE_PASSWORD
  info "verifying PyPI shows $VERSION as latest"
  for i in 1 2 3 4; do
    sleep 30
    LATEST="$(curl -fsSL "https://pypi.org/pypi/$PYPI_PROJECT/json" | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])")"
    echo "  attempt $i: latest=$LATEST"
    [ "$LATEST" = "$VERSION" ] && break
    [ "$i" = 4 ] && die "PyPI still does not show $VERSION as latest after 2 minutes"
  done
  info "PyPI live: https://pypi.org/project/$PYPI_PROJECT/$VERSION/"
fi

# ---------- 7. GitHub push ----------
if [ "$SKIP_PUSH" = 1 ]; then
  warn "skipping GitHub push (--skip-push)"
else
  MANIFEST="$(mktemp)"
  find . -type f \
    -not -path './.git/*' -not -path '*/__pycache__/*' \
    -not -path './.venv-*/*' -not -path './dist/*' -not -path './build/*' \
    -not -path './target/*' -not -path './kabir_lang.egg-info/*' \
    -not -path './.planning/*' -not -path '*/node_modules/*' \
    -not -name '*.pyc' -not -name '*.vsix' \
    -not -name '*.png' -not -name '*.jpg' -not -name '*.jpeg' \
    -not -name '*.gif' -not -name '*.ico' -not -name '*.mp3' \
    -not -name '*.wav' -not -name '*.mid' -not -name '*.pdf' \
    -not -name '*.zip' -not -name '*.tar' -not -name '*.gz' \
    -print0 | sed -z 's|^\./||' > "$MANIFEST"
  COUNT="$(python3 -c "print(open('$MANIFEST','rb').read().count(b'\x00'))")"
  info "pushing snapshot ($COUNT files) to GitHub $OWNER/$REPO ($BRANCH)"
  info "approve the push card when it appears"
  /opt/hatch/bin/github push-files --owner "$OWNER" --repo "$REPO" --branch "$BRANCH" \
    --message "Kabir $VERSION" --source-dir "$ROOT" --paths-file "$MANIFEST"
  rm -f "$MANIFEST"
  info "verifying GitHub head"
  for i in 1 2 3; do
    sleep 15
    HEAD_MSG="$(curl -fsSL "https://api.github.com/repos/$OWNER/$REPO/commits/$BRANCH" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['sha'][:9] + ' ' + d['commit']['message'].splitlines()[0])")"
    echo "  attempt $i: $HEAD_MSG"
    case "$HEAD_MSG" in *"Kabir $VERSION"*) break ;; esac
    [ "$i" = 3 ] && die "GitHub head does not show Kabir $VERSION after 45s"
  done
  info "GitHub live: https://github.com/$OWNER/$REPO"
fi

# ---------- 8. clean-venv install check ----------
# Normally installs from PyPI (proves the upload). When the upload was
# skipped, installs the locally built wheel instead (same artifact).
info "fresh install check in a clean venv"
rm -rf "$VERIFY_VENV"
python3 -m venv "$VERIFY_VENV"
if [ "$SKIP_PYPI" = 1 ]; then
  warn "upload skipped — installing the local wheel instead of PyPI"
  "$VERIFY_VENV/bin/pip" install -q "dist/kabir_lang-$VERSION-py3-none-any.whl"
else
  "$VERIFY_VENV/bin/pip" install -q "$PYPI_PROJECT"
fi
INSTALLED="$("$VERIFY_VENV/bin/kabir" --version)"
echo "  installed: $INSTALLED"
[ "$INSTALLED" = "kabir $VERSION" ] || die "clean install reports '$INSTALLED', expected 'kabir $VERSION'"
(cd /tmp && "$VERIFY_VENV/bin/kabir" run "$ROOT/examples/hello.kbr")

echo
if [ "$SKIP_PYPI" = 1 ] || [ "$SKIP_PUSH" = 1 ]; then
  echo "DONE — Kabir $VERSION prepared locally (some steps skipped):"
else
  echo "DONE — Kabir $VERSION released:"
fi
[ "$SKIP_PYPI" = 0 ] && echo "  PyPI:   https://pypi.org/project/$PYPI_PROJECT/$VERSION/"
[ "$SKIP_PUSH" = 0 ] && echo "  GitHub: https://github.com/$OWNER/$REPO"
echo "  Install: pip install $PYPI_PROJECT  (or: pip install --upgrade $PYPI_PROJECT)"
