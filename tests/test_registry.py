"""Package registry tests: [dependencies], kabir add, Kabir.lock,
vendoring, kabir publish (Kabir 3.0.0 Wave 2), plus the 5.0.0
Wave 2 extensions: index schema validation, ``@latest``,
transitive dependencies, publish validation and ``stage_publish``
(end-to-end against local test registries only).

All registry tests use local temporary registry directories (or a
local HTTP server serving one); no real registry or network is ever
contacted.
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kabir.errors import KabirError
from kabir.interpreter import Interpreter
from kabir.project import (add_package, new_project, parse_manifest,
                           publish_package, read_lockfile, read_manifest,
                           stage_publish, validate_index, _read_index)

GREET_KBR = (
    'func greet(who) {\n'
    '    return "Hello, " + who + "!"\n'
    '}\n'
)

OTHER_KBR = 'func other() {\n    return 42\n}\n'


def make_tarball(files):
    """Build a deterministic .tar.gz from {arcname: text}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname in sorted(files):
            data = files[arcname].encode("utf-8")
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_registry(tmp, packages, extras=None):
    """Create a local registry dir in *tmp*.

    *packages*: {name: {version: (tarball_bytes, sha256_or_None)}}.
    *extras*: optional {(name, version): {extra index fields}} --
    e.g. ``{("greet", "1.0.0"): {"dependencies": {"shout": "0.1.0"}}}``.
    Returns the registry directory Path. Tarball files land under
    ``packages/<name>-<version>.tar.gz`` and index.json points at
    them by relative url.
    """
    registry = Path(tmp) / "registry"
    pkg_dir = registry / "packages"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    index = {"packages": {}}
    for name, versions in packages.items():
        index["packages"][name] = {}
        for version, (data, sha256) in versions.items():
            filename = "%s-%s.tar.gz" % (name, version)
            (pkg_dir / filename).write_bytes(data)
            entry = {"url": "packages/%s" % filename}
            if sha256 is not None:
                entry["sha256"] = sha256
            if extras:
                entry.update(extras.get((name, version), {}))
            index["packages"][name][version] = entry
    (registry / "index.json").write_text(json.dumps(index, indent=2))
    return registry


def greet_tarball():
    data = make_tarball({"greet.kbr": GREET_KBR})
    return data, hashlib.sha256(data).hexdigest()


def no_env():
    """Context manager that removes KABIR_REGISTRY for its duration."""
    ctx = mock.patch.dict(os.environ)
    ctx.__enter__()
    os.environ.pop("KABIR_REGISTRY", None)
    return ctx


class TestDependenciesManifest(unittest.TestCase):
    def test_dependencies_parsed(self):
        text = ('[package]\n'
                'name = "app"\n'
                'version = "1.2.3"\n'
                '\n'
                '[dependencies]\n'
                'greet = "1.0.0"\n'
                'other = "2.3.4"\n')
        manifest = parse_manifest(text)
        self.assertEqual(manifest["name"], "app")
        self.assertEqual(manifest["dependencies"],
                         {"greet": "1.0.0", "other": "2.3.4"})

    def test_dependencies_default_empty(self):
        manifest = parse_manifest('[package]\nname = "bare"\n')
        self.assertEqual(manifest["dependencies"], {})

    def test_dependencies_before_package(self):
        # Section order does not matter; [package] must still exist.
        text = ('[dependencies]\ngreet = "1.0.0"\n\n'
                '[package]\nname = "app"\n')
        manifest = parse_manifest(text)
        self.assertEqual(manifest["dependencies"], {"greet": "1.0.0"})

    def test_bad_dependency_versions_rejected(self):
        bad_versions = ["^1.0.0", ">=1.0.0", "~1.0.0", "1.0", "1",
                        "*", "1.0.x", "latest", ""]
        for version in bad_versions:
            with self.subTest(version=version):
                text = ('[package]\nname = "app"\n\n'
                        '[dependencies]\ngreet = "%s"\n' % version)
                with self.assertRaises(KabirError) as ctx:
                    parse_manifest(text)
                self.assertIn("exact", str(ctx.exception))

    def test_bad_dependency_names_and_duplicates(self):
        bad = [
            '[package]\nname = "app"\n\n[dependencies]\n1bad = "1.0.0"\n',
            '[package]\nname = "app"\n\n[dependencies]\n'
            'greet = "1.0.0"\ngreet = "1.0.1"\n',
            '[package]\nname = "app"\n\n[dependencies]\n'
            'greet = "1.0.0"\n[dependencies]\n',
            '[package]\nname = "app"\n\n[dependencies]\n'
            'greet = ["1.0.0"]\n',
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(KabirError):
                    parse_manifest(text)

    def test_unknown_section_still_rejected(self):
        with self.assertRaises(KabirError):
            parse_manifest('[package]\nname = "app"\n[other]\nx = "y"\n')


class TestAddPackage(unittest.TestCase):
    def test_add_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            result = add_package("greet", "1.0.0", proj, registry)
            self.assertEqual(result["name"], "greet")
            self.assertEqual(result["version"], "1.0.0")
            self.assertEqual(result["sha256"], digest)
            # Vendored layout: vendor/<name>/<version>/...
            vendored = proj / "vendor" / "greet" / "1.0.0" / "greet.kbr"
            self.assertEqual(result["vendor_dir"],
                             str(proj / "vendor" / "greet" / "1.0.0"))
            self.assertTrue(vendored.is_file())
            self.assertEqual(vendored.read_text(), GREET_KBR)
            # Manifest records the dependency.
            manifest = read_manifest(proj / "Kabir.toml")
            self.assertEqual(manifest["dependencies"], {"greet": "1.0.0"})
            self.assertIn('greet = "1.0.0"',
                          (proj / "Kabir.toml").read_text())
            # Lockfile records name, version and sha256.
            locked = read_lockfile(proj)
            self.assertEqual(locked, {"greet": {"version": "1.0.0",
                                                "sha256": digest}})
            lock_text = json.loads((proj / "Kabir.lock").read_text())
            self.assertEqual(lock_text["packages"]["greet"]["sha256"],
                             digest)

    def test_add_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                add_package("greet", "1.0.0", proj, registry)
            self.assertEqual(buf.getvalue(), "")

    def test_add_idempotent_and_updates_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            data1, digest1 = greet_tarball()
            data2, digest2 = (make_tarball({"greet.kbr": OTHER_KBR}),
                              None)
            digest2 = hashlib.sha256(data2).hexdigest()
            registry = make_registry(tmp, {"greet": {
                "1.0.0": (data1, digest1),
                "1.1.0": (data2, digest2),
            }})
            proj = new_project("my_app", cwd=tmp)
            add_package("greet", "1.0.0", proj, registry)
            # Re-adding the same version re-vendors cleanly.
            add_package("greet", "1.0.0", proj, registry)
            self.assertEqual(
                (proj / "vendor" / "greet" / "1.0.0" / "greet.kbr")
                .read_text(), GREET_KBR)
            # Adding another version keeps both vendored side by side.
            add_package("greet", "1.1.0", proj, registry)
            self.assertEqual(
                (proj / "vendor" / "greet" / "1.1.0" / "greet.kbr")
                .read_text(), OTHER_KBR)
            manifest = read_manifest(proj / "Kabir.toml")
            self.assertEqual(manifest["dependencies"], {"greet": "1.1.0"})
            locked = read_lockfile(proj)
            self.assertEqual(locked["greet"]["version"], "1.1.0")
            self.assertEqual(locked["greet"]["sha256"], digest2)

    def test_add_second_package_merges_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            data_o, digest_o = (make_tarball({"other.kbr": OTHER_KBR}),
                                None)
            digest_o = hashlib.sha256(data_o).hexdigest()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
                "other": {"0.2.0": (data_o, digest_o)},
            })
            proj = new_project("my_app", cwd=tmp)
            add_package("greet", "1.0.0", proj, registry)
            add_package("other", "0.2.0", proj, registry)
            self.assertEqual(read_lockfile(proj), {
                "greet": {"version": "1.0.0", "sha256": digest_g},
                "other": {"version": "0.2.0", "sha256": digest_o},
            })
            manifest = read_manifest(proj / "Kabir.toml")
            self.assertEqual(manifest["dependencies"], {
                "greet": "1.0.0", "other": "0.2.0"})

    def test_add_via_env_var_and_string_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            # registry=None reads KABIR_REGISTRY.
            with mock.patch.dict(os.environ,
                                 {"KABIR_REGISTRY": str(registry)}):
                add_package("greet", "1.0.0", proj)
            self.assertTrue(
                (proj / "vendor" / "greet" / "1.0.0" / "greet.kbr")
                .is_file())
            # A plain string path works too.
            proj2 = new_project("other_app", cwd=tmp)
            add_package("greet", "1.0.0", proj2, str(registry))
            self.assertTrue(
                (proj2 / "vendor" / "greet" / "1.0.0" / "greet.kbr")
                .is_file())

    def test_add_via_http_registry(self):
        # A URL base (KABIR_REGISTRY=http://...) served by a local
        # HTTP server: index.json + package fetched over urllib.
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            handler = partial(SimpleHTTPRequestHandler,
                              directory=str(registry))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % server.server_address[1]
                proj = new_project("my_app", cwd=tmp)
                result = add_package("greet", "1.0.0", proj, base)
                self.assertEqual(result["sha256"], digest)
                self.assertEqual(
                    (proj / "vendor" / "greet" / "1.0.0" / "greet.kbr")
                    .read_text(), GREET_KBR)
                self.assertEqual(read_lockfile(proj)["greet"]["sha256"],
                                 digest)
            finally:
                server.shutdown()
                server.server_close()

    def test_add_unknown_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("nope", "1.0.0", proj, registry)
            self.assertIn('Package "nope" not found', str(ctx.exception))
            self.assertFalse((proj / "vendor").exists())
            self.assertFalse((proj / "Kabir.lock").exists())

    def test_add_unknown_version_lists_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "9.9.9", proj, registry)
            self.assertIn("has no version 9.9.9", str(ctx.exception))
            self.assertIn("1.0.0", str(ctx.exception))
            self.assertFalse((proj / "vendor").exists())

    def test_add_bad_sha256_refuses_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, "0" * 64)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn("sha256 mismatch", str(ctx.exception))
            # Nothing installed, nothing recorded.
            self.assertFalse((proj / "vendor").exists())
            self.assertFalse((proj / "Kabir.lock").exists())
            self.assertEqual(
                read_manifest(proj / "Kabir.toml")["dependencies"], {})

    def test_add_without_sha256_records_download_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, None)}})
            proj = new_project("my_app", cwd=tmp)
            result = add_package("greet", "1.0.0", proj, registry)
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(read_lockfile(proj)["greet"]["sha256"],
                             digest)

    def test_add_default_registry_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            with no_env():
                with self.assertRaises(KabirError) as ctx:
                    add_package("greet", "1.0.0", proj)
            self.assertIn(
                "registry not set up yet — set KABIR_REGISTRY to a "
                "local registry dir", str(ctx.exception))
            self.assertFalse((proj / "vendor").exists())

    def test_add_missing_registry_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj,
                            Path(tmp) / "no-such-registry")
            self.assertIn("does not exist", str(ctx.exception))

    def test_add_missing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "empty-registry"
            registry.mkdir()
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn("index not found", str(ctx.exception))

    def test_add_malformed_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            registry.mkdir()
            (registry / "index.json").write_text('{"nope": true}')
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError):
                add_package("greet", "1.0.0", proj, registry)
            (registry / "index.json").write_text('not json at all')
            with self.assertRaises(KabirError):
                add_package("greet", "1.0.0", proj, registry)

    def test_add_non_tarball_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (b"this is not a tarball",
                                          None)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn("not a valid tarball", str(ctx.exception))
            self.assertFalse((proj / "vendor").exists())

    def test_add_unsafe_tarball_path_refused(self):
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evil.kbr")
            payload = b"evil"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (evil.getvalue(), None)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn("unsafe path", str(ctx.exception))
            self.assertFalse((Path(tmp) / "evil.kbr").exists())

    def test_add_bad_name_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError):
                add_package("1bad", "1.0.0", proj, tmp)
            with self.assertRaises(KabirError):
                add_package("greet", "^1.0.0", proj, tmp)
            with self.assertRaises(KabirError):
                add_package("greet", "1.0", proj, tmp)

    def test_add_missing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KabirError):
                add_package("greet", "1.0.0", Path(tmp) / "nope", tmp)

    def test_add_preserves_manifest_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            (proj / "Kabir.toml").write_text(
                '# my project\n[package]\nname = "my_app"\n'
                'version = "0.1.0"\n# trailing note\n')
            add_package("greet", "1.0.0", proj, registry)
            text = (proj / "Kabir.toml").read_text()
            self.assertIn("# my project", text)
            self.assertIn("# trailing note", text)
            self.assertIn('[dependencies]\ngreet = "1.0.0"', text)


class TestPublishPackage(unittest.TestCase):
    def test_publish_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            result = publish_package(proj)
            self.assertEqual(result["name"], "my_app")
            self.assertEqual(result["version"], "0.1.0")
            tarball = Path(result["tarball"])
            self.assertEqual(tarball,
                             proj / "target" / "my_app-0.1.0.tar.gz")
            self.assertTrue(tarball.is_file())
            digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
            self.assertEqual(result["sha256"], digest)
            # Tarball contents: manifest + src.
            with tarfile.open(tarball, "r:*") as tar:
                names = sorted(tar.getnames())
            self.assertEqual(names, ["Kabir.toml", "src/main.kbr"])
            self.assertEqual(result["files"],
                             ["Kabir.toml", "src/main.kbr"])
            # Exact registry-entry JSON.
            self.assertEqual(result["registry_entry"], {
                "packages": {
                    "my_app": {
                        "0.1.0": {
                            "url": "packages/my_app-0.1.0.tar.gz",
                            "sha256": digest,
                        },
                    },
                },
            })
            # Steps tell the user how to submit it, and say nothing
            # about pushing from here.
            self.assertTrue(result["steps"])
            steps_text = "\n".join(result["steps"])
            self.assertIn("index.json", steps_text)
            self.assertIn("kabir add my_app@0.1.0", steps_text)
            # Publishing does not touch the manifest.
            self.assertEqual(
                read_manifest(proj / "Kabir.toml")["dependencies"], {})

    def test_publish_deterministic_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            first = publish_package(proj)
            second = publish_package(proj)
            self.assertEqual(first["sha256"], second["sha256"])

    def test_publish_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                publish_package(proj)
            self.assertEqual(buf.getvalue(), "")

    def test_publish_round_trip_into_registry(self):
        # A published tarball can be added back from a registry.
        with tempfile.TemporaryDirectory() as tmp:
            lib = new_project("greet", cwd=tmp)
            (lib / "src" / "main.kbr").write_text(GREET_KBR)
            published = publish_package(lib)
            tarball = Path(published["tarball"])
            data = tarball.read_bytes()
            registry = make_registry(
                tmp, {"greet": {"0.1.0": (data, published["sha256"])}})
            app = new_project("my_app", cwd=tmp)
            result = add_package("greet", "0.1.0", app, registry)
            self.assertEqual(result["sha256"], published["sha256"])
            self.assertEqual(
                (app / "vendor" / "greet" / "0.1.0" / "src" / "main.kbr")
                .read_text(), GREET_KBR)

    def test_publish_missing_manifest_or_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(KabirError):
                publish_package(empty)
            proj = new_project("my_app", cwd=tmp)
            (proj / "src" / "main.kbr").unlink()
            with self.assertRaises(KabirError):
                publish_package(proj)


class TestReadLockfile(unittest.TestCase):
    def test_no_lockfile_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_lockfile(Path(tmp)), {})

    def test_malformed_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Kabir.lock").write_text('{"packages": []}')
            with self.assertRaises(KabirError):
                read_lockfile(Path(tmp))
            (Path(tmp) / "Kabir.lock").write_text('not json')
            with self.assertRaises(KabirError):
                read_lockfile(Path(tmp))

class TestDescriptionManifest(unittest.TestCase):
    def test_description_parsed_and_defaults_empty(self):
        text = ('[package]\nname = "app"\nversion = "1.0.0"\n'
                'description = "Does things."\n')
        manifest = parse_manifest(text)
        self.assertEqual(manifest["description"], "Does things.")
        self.assertEqual(parse_manifest('[package]\nname = "bare"\n')
                         ["description"], "")

    def test_description_must_be_string(self):
        with self.assertRaises(KabirError):
            parse_manifest('[package]\nname = "app"\n'
                           'description = ["not", "a", "string"]\n')


class TestIndexSchema(unittest.TestCase):
    def _good_index(self):
        return {"packages": {"greet": {"1.0.0": {
            "url": "packages/greet-1.0.0.tar.gz",
            "sha256": "a" * 64,
            "description": "Say hello.",
            "dependencies": {"shout": "0.1.0"}},
            "1.1.0": {"url": "packages/greet-1.1.0.tar.gz",
                      "sha256": "b" * 64}}}}

    def test_valid_full_and_minimal_entries(self):
        index = self._good_index()
        self.assertIs(validate_index(index), index)

    def test_unknown_extra_keys_ignored(self):
        index = self._good_index()
        index["packages"]["greet"]["1.0.0"]["future_field"] = True
        index["registry_version"] = 1
        validate_index(index)

    def test_missing_packages_rejected(self):
        for bad in ({}, {"packages": []}, {"packages": {"greet": []}}):
            with self.subTest(bad=bad):
                with self.assertRaises(KabirError):
                    validate_index(bad)

    def test_bad_names_versions_and_urls_rejected(self):
        cases = [
            ({"packages": {"1bad": {"1.0.0": {"url": "x"}}}},
             "package name"),
            ({"packages": {"greet": {"1.0": {"url": "x"}}}},
             "version key"),
            ({"packages": {"greet": {"1.0.0": {"sha256": "a" * 64}}}},
             "missing url"),
            ({"packages": {"greet": {"1.0.0": {"url": ""}}}},
             "empty url"),
            ({"packages": {"greet": {"1.0.0": "not-a-dict"}}},
             "entry not a dict"),
        ]
        for bad, _label in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(KabirError):
                    validate_index(bad)

    def test_bad_sha256_description_dependencies_rejected(self):
        base = lambda entry: {"packages": {"greet": {"1.0.0": entry}}}
        bad_entries = [
            {"url": "x", "sha256": "xyz"},          # not hex
            {"url": "x", "sha256": "A" * 64},       # not lowercase
            {"url": "x", "sha256": "a" * 63},       # too short
            {"url": "x", "description": 42},       # not a string
            {"url": "x", "dependencies": []},      # not a mapping
            {"url": "x", "dependencies": {"1bad": "1.0.0"}},
            {"url": "x", "dependencies": {"shout": "^1.0.0"}},
            {"url": "x", "dependencies": {"shout": "1.0"}},
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                with self.assertRaises(KabirError):
                    validate_index(base(entry))

    def test_add_rejects_malformed_entry_fields(self):
        # A bad entry is caught at index-read time, before any
        # download is attempted.
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry"
            registry.mkdir()
            index = {"packages": {"greet": {"1.0.0": {
                "url": "packages/greet-1.0.0.tar.gz",
                "sha256": "not-a-digest"}}}}
            (registry / "index.json").write_text(json.dumps(index))
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn("sha256", str(ctx.exception))


class TestAddLatest(unittest.TestCase):
    def test_latest_picks_highest_version_numerically(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(tmp, {"greet": {
                "1.9.0": (data, digest),
                "1.10.0": (data, digest),
                "0.9.0": (data, digest),
            }})
            proj = new_project("my_app", cwd=tmp)
            result = add_package("greet", "latest", proj, registry)
            self.assertEqual(result["version"], "1.10.0")
            self.assertEqual(
                read_manifest(proj / "Kabir.toml")["dependencies"],
                {"greet": "1.10.0"})
            self.assertEqual(read_lockfile(proj)["greet"]["version"],
                             "1.10.0")

    def test_latest_unknown_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError):
                add_package("nope", "latest", proj, registry)


def _shout_tarball():
    data = make_tarball({"shout.kbr":
                         'func shout(who) {\n'
                         '    return greet(who) + "!!!"\n'
                         '}\n'})
    return data, hashlib.sha256(data).hexdigest()


class TestTransitiveDependencies(unittest.TestCase):
    def test_transitive_dep_installed_and_locked_not_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            data_s, digest_s = _shout_tarball()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
                "shout": {"0.1.0": (data_s, digest_s)},
            }, extras={("greet", "1.0.0"): {
                "dependencies": {"shout": "0.1.0"}}})
            proj = new_project("my_app", cwd=tmp)
            result = add_package("greet", "1.0.0", proj, registry)
            self.assertEqual(result["version"], "1.0.0")
            # Both vendored.
            self.assertTrue(
                (proj / "vendor" / "greet" / "1.0.0" / "greet.kbr")
                .is_file())
            self.assertTrue(
                (proj / "vendor" / "shout" / "0.1.0" / "shout.kbr")
                .is_file())
            # Only the direct dependency is in the manifest.
            self.assertEqual(
                read_manifest(proj / "Kabir.toml")["dependencies"],
                {"greet": "1.0.0"})
            # Both are in the lockfile.
            self.assertEqual(read_lockfile(proj), {
                "greet": {"version": "1.0.0", "sha256": digest_g},
                "shout": {"version": "0.1.0", "sha256": digest_s},
            })

    def test_diamond_and_cycle_install_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            data_s, digest_s = _shout_tarball()
            data_t, digest_t = (make_tarball(
                {"top.kbr": 'func top() {\n    return 1\n}\n'}),
                None)
            digest_t = hashlib.sha256(data_t).hexdigest()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
                "shout": {"0.1.0": (data_s, digest_s)},
                "top": {"1.0.0": (data_t, digest_t)},
            }, extras={
                ("greet", "1.0.0"): {"dependencies": {"shout": "0.1.0"}},
                ("top", "1.0.0"): {"dependencies": {
                    "greet": "1.0.0", "shout": "0.1.0"}},
            })
            proj = new_project("my_app", cwd=tmp)
            add_package("top", "1.0.0", proj, registry)
            locked = read_lockfile(proj)
            self.assertEqual(set(locked), {"top", "greet", "shout"})
            self.assertEqual(locked["shout"]["sha256"], digest_s)

            # A dependency cycle (a -> b -> a) installs both once.
            data_a, digest_a = (make_tarball(
                {"a.kbr": 'func a() {\n    return 1\n}\n'}), None)
            digest_a = hashlib.sha256(data_a).hexdigest()
            data_b, digest_b = (make_tarball(
                {"b.kbr": 'func b() {\n    return 2\n}\n'}), None)
            digest_b = hashlib.sha256(data_b).hexdigest()
            registry2 = make_registry(tmp, {
                "a_pkg": {"1.0.0": (data_a, digest_a)},
                "b_pkg": {"1.0.0": (data_b, digest_b)},
            }, extras={
                ("a_pkg", "1.0.0"): {"dependencies": {"b_pkg": "1.0.0"}},
                ("b_pkg", "1.0.0"): {"dependencies": {"a_pkg": "1.0.0"}},
            })
            proj2 = new_project("other_app", cwd=tmp)
            add_package("a_pkg", "1.0.0", proj2, registry2)
            self.assertEqual(set(read_lockfile(proj2)),
                             {"a_pkg", "b_pkg"})

    def test_transitive_version_conflict_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            data_s1, digest_s1 = _shout_tarball()
            data_s2 = make_tarball({"shout.kbr": OTHER_KBR})
            digest_s2 = hashlib.sha256(data_s2).hexdigest()
            data_x, digest_x = (make_tarball(
                {"x.kbr": 'func x() {\n    return 1\n}\n'}), None)
            digest_x = hashlib.sha256(data_x).hexdigest()
            data_y, digest_y = (make_tarball(
                {"y.kbr": 'func y() {\n    return 2\n}\n'}), None)
            digest_y = hashlib.sha256(data_y).hexdigest()
            data_t, digest_t = (make_tarball(
                {"top.kbr": 'func top() {\n    return 3\n}\n'}), None)
            digest_t = hashlib.sha256(data_t).hexdigest()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
                "shout": {"0.1.0": (data_s1, digest_s1),
                          "0.2.0": (data_s2, digest_s2)},
                "x_pkg": {"1.0.0": (data_x, digest_x)},
                "y_pkg": {"1.0.0": (data_y, digest_y)},
                "top": {"1.0.0": (data_t, digest_t)},
            }, extras={
                ("greet", "1.0.0"): {"dependencies": {"shout": "0.1.0"}},
                ("x_pkg", "1.0.0"): {"dependencies": {"shout": "0.1.0"}},
                ("y_pkg", "1.0.0"): {"dependencies": {"shout": "0.2.0"}},
                ("top", "1.0.0"): {"dependencies": {
                    "x_pkg": "1.0.0", "y_pkg": "1.0.0"}},
            })
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("top", "1.0.0", proj, registry)
            self.assertIn("Version conflict", str(ctx.exception))
            self.assertIn("shout", str(ctx.exception))

    def test_missing_transitive_dep_names_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
            }, extras={("greet", "1.0.0"): {
                "dependencies": {"ghost": "1.0.0"}}})
            proj = new_project("my_app", cwd=tmp)
            with self.assertRaises(KabirError) as ctx:
                add_package("greet", "1.0.0", proj, registry)
            self.assertIn('Package "ghost" not found', str(ctx.exception))


class TestPublishEntryFields(unittest.TestCase):
    def test_publish_entry_has_description_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "my_app"\nversion = "1.2.0"\n'
                'description = "My test app."\n'
                'authors = ["Afrain Kabir Rafid"]\n'
                '\n[dependencies]\ngreet = "1.0.0"\n')
            result = publish_package(proj)
            self.assertEqual(result["description"], "My test app.")
            self.assertEqual(result["dependencies"], {"greet": "1.0.0"})
            self.assertEqual(result["registry_entry"], {
                "packages": {"my_app": {"1.2.0": {
                    "url": "packages/my_app-1.2.0.tar.gz",
                    "sha256": result["sha256"],
                    "description": "My test app.",
                    "dependencies": {"greet": "1.0.0"},
                }}}})

    def test_publish_without_description_or_deps_stays_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            result = publish_package(proj)
            entry = result["registry_entry"]["packages"]["my_app"]["0.1.0"]
            self.assertEqual(set(entry), {"url", "sha256"})

    def test_publish_rejects_non_exact_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            (proj / "Kabir.toml").write_text(
                '[package]\nname = "my_app"\nversion = "1.0"\n')
            with self.assertRaises(KabirError) as ctx:
                publish_package(proj)
            self.assertIn("not an exact", str(ctx.exception))


class TestStagePublish(unittest.TestCase):
    def test_stage_fresh_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            with no_env():
                result = stage_publish(proj)
            stage_dir = Path(result["stage_dir"])
            self.assertEqual(stage_dir,
                             proj / "target" / "registry-stage")
            staged_index = json.loads(
                Path(result["staged_index"]).read_text())
            self.assertEqual(
                staged_index["packages"]["my_app"]["0.1.0"]["sha256"],
                result["sha256"])
            self.assertTrue(Path(result["staged_tarball"]).is_file())
            self.assertEqual(
                Path(result["staged_tarball"]).read_bytes(),
                Path(result["tarball"]).read_bytes())
            # The staged index re-reads as a valid registry index.
            reread = _read_index(("local", stage_dir))
            self.assertEqual(reread["packages"]["my_app"]["0.1.0"]
                             ["sha256"], result["sha256"])
            # Commands tell the user how to push, and name the
            # planned index repo.
            commands = "\n".join(result["commands"])
            self.assertIn("git clone", commands)
            self.assertIn("kabir-registry", commands)
            self.assertIn("git push", commands)
            self.assertTrue(result["steps"])

    def test_stage_merges_existing_local_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            lib = new_project("farewell", cwd=tmp)
            (lib / "src" / "main.kbr").write_text(
                'func farewell(who) {\n'
                '    return "Goodbye, " + who + "!"\n'
                '}\n')
            result = stage_publish(lib, registry=registry)
            staged = json.loads(
                Path(result["staged_index"]).read_text())
            # The old package survives, the new one is added.
            self.assertEqual(
                staged["packages"]["greet"]["1.0.0"]["sha256"], digest)
            self.assertEqual(
                staged["packages"]["farewell"]["0.1.0"]["sha256"],
                result["sha256"])
            # Local-registry commands copy into the registry dir.
            commands = "\n".join(result["commands"])
            self.assertIn("cp ", commands)
            self.assertIn(str(registry), commands)
            self.assertIn("git push", commands)

    def test_stage_refuses_duplicate_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            lib = new_project("greet", cwd=tmp)
            (lib / "Kabir.toml").write_text(
                '[package]\nname = "greet"\nversion = "1.0.0"\n')
            with self.assertRaises(KabirError) as ctx:
                stage_publish(lib, registry=registry)
            self.assertIn("already in the registry", str(ctx.exception))

    def test_stage_reads_index_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, digest = greet_tarball()
            registry = make_registry(
                tmp, {"greet": {"1.0.0": (data, digest)}})
            handler = partial(SimpleHTTPRequestHandler,
                              directory=str(registry))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % server.server_address[1]
                lib = new_project("farewell", cwd=tmp)
                result = stage_publish(lib, registry=base)
                staged = json.loads(
                    Path(result["staged_index"]).read_text())
                self.assertIn("greet", staged["packages"])
                self.assertIn("farewell", staged["packages"])
            finally:
                server.shutdown()
                server.server_close()

    def test_stage_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = new_project("my_app", cwd=tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                with no_env():
                    stage_publish(proj)
            self.assertEqual(buf.getvalue(), "")

    def test_end_to_end_add_run_publish_add_from_stage(self):
        # The full loop against LOCAL test registries only:
        # add from a test index -> run the vendored code ->
        # publish a new package -> stage it -> add from the
        # staged index.
        with tempfile.TemporaryDirectory() as tmp:
            data_g, digest_g = greet_tarball()
            data_s, digest_s = _shout_tarball()
            registry = make_registry(tmp, {
                "greet": {"1.0.0": (data_g, digest_g)},
                "shout": {"0.1.0": (data_s, digest_s)},
            }, extras={
                ("greet", "1.0.0"): {
                    "description": "Say hello.",
                    "dependencies": {"shout": "0.1.0"}},
            })
            # 1. Add from the test index via KABIR_REGISTRY.
            app = new_project("my_app", cwd=tmp)
            with mock.patch.dict(
                    os.environ, {"KABIR_REGISTRY": str(registry)}):
                result = add_package("greet", "1.0.0", app)
            self.assertEqual(result["sha256"], digest_g)
            vendored = (app / "vendor" / "greet" / "1.0.0"
                        / "greet.kbr").read_text()
            self.assertEqual(vendored, GREET_KBR)
            # 2. The vendored code actually runs.
            source = vendored + '\nprint(greet("World"))\n'
            interp = Interpreter(source, filename="<test>")
            buf = io.StringIO()
            with redirect_stdout(buf):
                interp.run_source(source, filename="<test>")
            self.assertEqual(buf.getvalue(), "Hello, World!\n")
            # 3. Publish a new package and stage it into the
            #    same registry.
            lib = new_project("farewell", cwd=tmp)
            (lib / "Kabir.toml").write_text(
                '[package]\nname = "farewell"\nversion = "2.0.0"\n'
                'description = "Say goodbye."\n')
            (lib / "src" / "main.kbr").write_text(
                'func farewell(who) {\n'
                '    return "Goodbye, " + who + "!"\n'
                '}\n')
            staged = stage_publish(lib, registry=registry)
            self.assertEqual(staged["name"], "farewell")
            self.assertEqual(staged["version"], "2.0.0")
            # 4. The staged index is a complete, working registry:
            #    re-read it and add the new package from it.
            stage_dir = Path(staged["stage_dir"])
            reread = _read_index(("local", stage_dir))
            self.assertEqual(
                reread["packages"]["farewell"]["2.0.0"]["sha256"],
                staged["sha256"])
            self.assertEqual(
                reread["packages"]["greet"]["1.0.0"]["sha256"],
                digest_g)
            app2 = new_project("second_app", cwd=tmp)
            result2 = add_package("farewell", "2.0.0", app2, stage_dir)
            self.assertEqual(result2["sha256"], staged["sha256"])
            self.assertEqual(
                (app2 / "vendor" / "farewell" / "2.0.0" / "src"
                 / "main.kbr").read_text(),
                'func farewell(who) {\n'
                '    return "Goodbye, " + who + "!"\n'
                '}\n')
            # 5. And the same staged index works over HTTP too.
            handler = partial(SimpleHTTPRequestHandler,
                              directory=str(stage_dir))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % server.server_address[1]
                app3 = new_project("third_app", cwd=tmp)
                result3 = add_package("farewell", "2.0.0", app3, base)
                self.assertEqual(result3["sha256"], staged["sha256"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
