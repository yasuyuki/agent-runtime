import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


from agent_runtime import handoff as receive


def git(directory, *args):
    return subprocess.run(["git", *args], cwd=directory, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.original_fetch = receive._fetch
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.dest = self.base / "dest"
        self.source.mkdir(); self.dest.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "config", "user.email", "test@example.invalid"); git(self.source, "config", "user.name", "test")
        # The configured anchor predates the shared document in production.
        (self.source / "anchor").write_text("anchor\n", encoding="utf-8")
        git(self.source, "add", "anchor"); git(self.source, "commit", "-qm", "anchor")
        self.initial = git(self.source, "rev-parse", "HEAD")
        (self.source / "shared.md").write_text("first\n", encoding="utf-8")
        git(self.source, "add", "shared.md"); git(self.source, "commit", "-qm", "initial shared")
        git(self.dest, "init", "-q", "-b", "main")
        git(self.dest, "config", "user.email", "test@example.invalid"); git(self.dest, "config", "user.name", "test")
        (self.dest / "HANDOFF.md").write_bytes(b"# local\r\nkeep\r\n")
        git(self.dest, "add", "HANDOFF.md"); git(self.dest, "commit", "-qm", "base")
        (self.dest / "rules").mkdir()
        self.binding()

    def tearDown(self):
        receive._fetch = self.original_fetch
        self.temp.cleanup()

    def binding(self, **changes):
        data = {"version": 1, "pair": "s1", "repository": self.source.as_uri().replace("file://", "https://example.invalid/"), "branch": "main", "document": "shared.md", "initial_revision": self.initial}
        data.update(changes)
        (self.dest / "rules" / "handoff-receive.json").write_text(json.dumps(data), encoding="utf-8")
        # Tests replace HTTPS fetch with local git transport only at the subprocess boundary.
        def local_fetch(root, binding, common):
            binding = dict(binding); binding["repository"] = str(self.source)
            temporary = Path(tempfile.mkdtemp(prefix="fixture-fetch-", dir=common))
            git(self.base, "init", "--bare", "-q", str(temporary))
            git(temporary, "fetch", "-q", "--no-tags", str(self.source), "+refs/heads/main:refs/heads/received")
            return temporary, git(temporary, "rev-parse", "refs/heads/received")
        receive._fetch = local_fetch

    def update_source(self, text):
        (self.source / "shared.md").write_text(text, encoding="utf-8")
        git(self.source, "add", "shared.md"); git(self.source, "commit", "-qm", "update")

    def test_installed_version_matches_distribution(self):
        import importlib.metadata
        import sys
        suffix = ".exe" if os.name == "nt" else ""
        entry = Path(sys.executable).parent / ("agent-runtime" + suffix)
        result = subprocess.run([str(entry), "--version"], cwd=self.base,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), importlib.metadata.version("agent-runtime"))

    def test_installed_console_module_and_fixed_source_entries(self):
        import sys
        suffix = ".exe" if os.name == "nt" else ""
        console = Path(sys.executable).parent / ("agent-handoff-receive" + suffix)
        entries = [[str(console)], [sys.executable, "-m", "agent_runtime.handoff"],
                   [sys.executable, str(Path(receive.__file__).resolve())]]
        unbound = self.base / "unbound"
        unbound.mkdir()
        for entry in entries:
            with self.subTest(entry=entry):
                result = subprocess.run([*entry, "--workspace", str(unbound)],
                                        cwd=unbound, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "not-configured")
                self.assertEqual(result.stderr, "")
                self.binding(repository="ssh://invalid/forbidden")
                failed = subprocess.run([*entry, "--workspace", str(self.dest)],
                                        cwd=unbound, text=True, capture_output=True)
                self.assertEqual(failed.returncode, 1, failed.stderr)
                self.assertEqual(json.loads(failed.stdout)["status"], "error")

    def test_fixed_cli_status_and_failure_exit(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receive.main(["--workspace", str(self.dest)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "updated")
        self.binding(repository="ssh://invalid/forbidden")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receive.main(["--workspace", str(self.dest)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_first_update_repeat_and_crlf_is_preserved(self):
        first = receive.receive(self.dest)
        self.assertEqual("updated", first["status"])
        data = (self.dest / "HANDOFF.md").read_bytes()
        self.assertIn(b"# local\r\nkeep\r\n", data)
        self.assertEqual("unchanged", receive.receive(self.dest)["status"])
        self.update_source("second\n")
        self.assertEqual("updated", receive.receive(self.dest)["status"])
        self.assertIn("second", (self.dest / "HANDOFF.md").read_text())

    def test_local_conflict_and_local_preservation(self):
        receive.receive(self.dest)
        path = self.dest / "HANDOFF.md"
        path.write_text(path.read_text().replace("first", "local edit"), encoding="utf-8")
        self.update_source("remote edit\n")
        self.assertEqual("conflict", receive.receive(self.dest)["status"])
        self.assertIn("local edit", path.read_text())
        # A repeated incoming body must not overwrite the local correction.
        (self.source / "shared.md").write_text("first\n", encoding="utf-8")
        git(self.source, "add", "shared.md"); git(self.source, "commit", "-qm", "repeat")
        self.assertEqual("unchanged", receive.receive(self.dest)["status"])
        self.assertIn("local edit", path.read_text())

    def test_rejects_bad_binding_payload_and_symlink(self):
        self.binding(repository="ssh://bad/x")
        self.assertEqual("error", receive.receive(self.dest)["status"])
        self.binding()
        (self.source / "shared.md").write_text("```sh\necho bad\n```\n", encoding="utf-8")
        git(self.source, "add", "shared.md"); git(self.source, "commit", "-qm", "bad")
        self.assertEqual("error", receive.receive(self.dest)["status"])
        (self.dest / "HANDOFF.md").unlink()
        try:
            (self.dest / "HANDOFF.md").symlink_to("elsewhere")
        except OSError:
            if os.name != "nt":
                raise
            return  # Windows hosts may disallow creation of symlinks.
        self.assertEqual("error", receive.receive(self.dest)["status"])

    def test_transport_failure_and_history_rejection_do_not_write(self):
        path = self.dest / "HANDOFF.md"
        before = path.read_bytes()
        original_fetch = receive._fetch
        receive._fetch = lambda *args: (_ for _ in ()).throw(receive.ReceiveError("fetch failed"))
        try:
            self.assertEqual("error", receive.receive(self.dest)["status"])
        finally:
            receive._fetch = original_fetch
        self.assertEqual(before, path.read_bytes())
        # An initial revision from unrelated history cannot authorize a fetch.
        other = self.base / "other"; other.mkdir(); git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.email", "test@example.invalid"); git(other, "config", "user.name", "test")
        (other / "x").write_text("x", encoding="utf-8"); git(other, "add", "x"); git(other, "commit", "-qm", "x")
        self.binding(initial_revision=git(other, "rev-parse", "HEAD"))
        self.assertEqual("error", receive.receive(self.dest)["status"])
        self.assertEqual(before, path.read_bytes())

    def test_late_writer_is_preserved(self):
        original = (self.dest / "HANDOFF.md").read_bytes()
        exchange_name = "_exchange_windows" if os.name == "nt" else "_exchange_linux"
        real = getattr(receive, exchange_name)
        def race(left, right, *extra):
            real(left, right, *extra)
            Path(right).write_bytes(b"late writer\n")
        setattr(receive, exchange_name, race)
        try:
            result = receive.receive(self.dest)
        finally:
            setattr(receive, exchange_name, real)
        self.assertEqual("concurrent-write", result["status"])
        self.assertEqual(b"late writer\n", (self.dest / "HANDOFF.md").read_bytes())
        self.assertTrue(Path(result["recovery"]).read_bytes() == original)

    def test_before_exchange_race_binding_change_and_dirty_index_are_preserved(self):
        path = self.dest / "HANDOFF.md"
        original = path.read_bytes()
        exchange_name = "_exchange_windows" if os.name == "nt" else "_exchange_linux"
        real = getattr(receive, exchange_name)
        def race(left, right, *extra):
            Path(right).write_bytes(b"writer before exchange\n")
            real(left, right, *extra)
        setattr(receive, exchange_name, race)
        try:
            result = receive.receive(self.dest)
        finally:
            setattr(receive, exchange_name, real)
        self.assertEqual("concurrent-write", result["status"])
        self.assertEqual(b"writer before exchange\n", Path(result["recovery"]).read_bytes())
        self.assertEqual(original, Path(result["original_recovery"]).read_bytes())
        # A receipt written by the failed exchange is not a successful receipt
        # on the next normal start. Restoring the displaced local version
        # preserves that competing edit before retrying the same receiver.
        self.assertEqual('error', receive.receive(self.dest)['status'])
        path.write_bytes(Path(result['recovery']).read_bytes())
        self.assertEqual('updated', receive.receive(self.dest)['status'])
        self.assertIn(b'writer before exchange\n', path.read_bytes())
        # Binding changes during the fetch prevent the prepared write.
        path.write_bytes(original)
        real_fetch = receive._fetch
        def changing_fetch(*args):
            repo, incoming = real_fetch(*args)
            binding = self.dest / "rules" / "handoff-receive.json"
            binding.write_text(binding.read_text() + " ", encoding="utf-8")
            return repo, incoming
        receive._fetch = changing_fetch
        try:
            self.assertEqual("error", receive.receive(self.dest)["status"])
        finally:
            receive._fetch = real_fetch
        self.assertEqual(original, path.read_bytes())
        # Receiver does not touch unrelated staged state or HEAD.
        (self.dest / "unrelated").write_text("dirty", encoding="utf-8")
        git(self.dest, "add", "unrelated")
        head = git(self.dest, "rev-parse", "HEAD")
        self.binding()
        receive.receive(self.dest)
        self.assertEqual(head, git(self.dest, "rev-parse", "HEAD"))
        self.assertIn("A  unrelated", subprocess.run(["git", "status", "--short"], cwd=self.dest, text=True, stdout=subprocess.PIPE).stdout)

    def test_malformed_duplicate_and_sibling_markers(self):
        path = self.dest / "HANDOFF.md"
        path.write_text("<!-- handoff-receive:s1 revision=%s -->\nbad" % self.initial, encoding="utf-8")
        self.assertEqual("error", receive.receive(self.dest)["status"])
        path.write_text("<!-- handoff-receive:s1 revision=%s -->\na\n<!-- /handoff-receive:s1 -->\n<!-- handoff-receive:s1 revision=%s -->\nb\n<!-- /handoff-receive:s1 -->" % (self.initial, self.initial), encoding="utf-8")
        self.assertEqual("error", receive.receive(self.dest)["status"])
        path.write_text("# local\n<!-- handoff-receive:other revision=%s -->\nx\n<!-- /handoff-receive:other -->\n" % self.initial, encoding="utf-8")
        self.assertEqual("updated", receive.receive(self.dest)["status"])
        self.assertIn("handoff-receive:other", path.read_text())

    def test_stale_delivery_and_echo_do_not_change_receipt(self):
        self.assertEqual('updated', receive.receive(self.dest)['status'])
        self.update_source('newer\n')
        self.assertEqual('updated', receive.receive(self.dest)['status'])
        before = (self.dest / 'HANDOFF.md').read_bytes()
        transport = receive._fetch
        def stale(*args):
            repo, _ = transport(*args)
            return repo, self.initial
        with patch.object(receive, '_fetch', stale):
            self.assertEqual('stale-or-diverged', receive.receive(self.dest)['status'])
        self.assertEqual(before, (self.dest / 'HANDOFF.md').read_bytes())
        git(self.source, 'commit', '--allow-empty', '-qm', 'same received content')
        self.assertEqual('unchanged', receive.receive(self.dest)['status'])
        self.assertEqual(before, (self.dest / 'HANDOFF.md').read_bytes())

    def test_publication_failures_preserve_candidates_and_retry(self):
        target = self.dest / 'HANDOFF.md'
        original = target.read_bytes()
        with patch.object(receive.os, 'fsync', side_effect=OSError('fsync refused')):
            self.assertEqual('write-failed', receive.receive(self.dest)['status'])
        self.assertEqual(original, target.read_bytes())
        exchange_name = '_exchange_windows' if os.name == 'nt' else '_exchange_linux'
        real = getattr(receive, exchange_name)
        def partial(*args):
            real(*args)
            raise OSError('injected partial replacement failure')
        with patch.object(receive, exchange_name, partial):
            outcome = receive.receive(self.dest)
            self.assertEqual('write-failed', outcome['status'])
        self.assertEqual(original, Path(outcome['recovery']).read_bytes())
        self.assertEqual(original, Path(outcome['original_recovery']).read_bytes())
        self.assertEqual('unchanged', receive.receive(self.dest)['status'])

    def test_production_fetch_keeps_refs_index_and_fetch_head(self):
        # Substitute only HTTPS transport for a disposable local Git endpoint;
        # exercise the production ls-remote/OID fetch/options and receiver.
        actual_fetch = self.original_fetch
        actual_run = receive._run
        observed = []
        def local_transport(argv, **kwargs):
            observed.append(argv)
            converted = [str(self.source) if item.startswith('https://') else item for item in argv]
            if 'protocol.https.allow=always' in converted:
                converted[converted.index('protocol.https.allow=always')] = 'protocol.file.allow=always'
            return actual_run(converted, **kwargs)
        head = git(self.dest, 'show-ref')
        index = git(self.dest, 'ls-files', '--stage')
        fetch_head = self.dest / '.git/FETCH_HEAD'
        fetch_head.write_bytes(b'preexisting fetch record\n')
        with patch.object(receive, '_fetch', actual_fetch), patch.object(receive, '_run', local_transport):
            self.assertEqual('updated', receive.receive(self.dest)['status'])
            self.assertEqual('unchanged', receive.receive(self.dest)['status'])
        self.assertEqual(head, git(self.dest, 'show-ref'))
        self.assertEqual(index, git(self.dest, 'ls-files', '--stage'))
        self.assertEqual(b'preexisting fetch record\n', fetch_head.read_bytes())
        self.assertTrue(any('--no-write-fetch-head' in argv for argv in observed))

    def test_unbound_non_git_and_unsafe_targets(self):
        self.assertEqual('not-configured', receive.receive(self.base)['status'])
        for changes in ({'document': '../outside.md'}, {'document': '/outside.md'},
                        {'command': 'execute'}, {'branch': '--upload-pack=evil'},
                        {'version': True}):
            self.binding(**changes)
            self.assertEqual('error', receive.receive(self.dest)['status'])


if __name__ == "__main__":
    unittest.main()
