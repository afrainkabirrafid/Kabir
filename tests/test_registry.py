"""Package registry tests: [dependencies], kabir add, Kabir.lock,
vendoring, kabir publish (Kabir 3.0.0 Wave 2).

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
from kabir.project import (add_package, new_project, parse_manifest,
                           publish_package, read_lockfile, read_manifest)

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


def make_registry(tmp, packages):
    """Create a local registry dir in *tmp*.

    *packages*: {name: {version: (tarball_bytes, sha256_or_None)}}.
    Returns the registry directory Path. Tarball files land under
    ``packages/<name>-<version>.tar.gz`` and index.json points at
    them by relative url.
    """
    registry = Path(tmp) / "registry"
    pkg_dir = registry / "packages"
    pkg_dir.mkdir(parents=True)
    index = {"packages": {}}
    for name, versions in packages.items():
        index["packages"][name] = {}
        for version, (data, sha256) in versions.items():
            filename = "%s-%s.tar.gz" % (name, version)
            (pkg_dir / filename).write_bytes(data)
            entry = {"url": "packages/%s" % filename}
            if sha256 is not None:
                entry["sha256"] = sha256
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


if __name__ == "__main__":
    unittest.main()
