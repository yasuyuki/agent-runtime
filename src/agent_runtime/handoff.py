"""Safely receive one fixed shared handoff fragment into ``HANDOFF.md``.

The binding is deliberately local and boring: this module never follows a
location, command, or repository supplied by the received Markdown.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PAIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MARK = re.compile(r"<!-- handoff-receive:([A-Za-z0-9][A-Za-z0-9._-]*) revision=([0-9a-f]+) -->\n?(.*?)<!-- /handoff-receive:\1 -->", re.S)


# The isolated launcher's existing SSH qualification uses this same bound.
# One deadline covers advertise and fetch. It is not a measured percentile.
BUDGET_SECONDS = 30
_STDERR_LIMIT = 512


class ReceiveError(RuntimeError):
    def __init__(self, message, stage="receive", reason="unknown", exit_code=None, applied=False):
        super().__init__(message)
        self.stage = stage
        self.reason = reason
        self.exit_code = exit_code
        self.applied = applied


def _result(status, **values):
    return {"status": status, **values}


def _redact(payload):
    if not payload:
        return ""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload
    text = re.sub(r"://[^/\s]+@", "://", text)
    text = re.sub(r"(?i)(authorization:\s*)\S+", r"\1", text)
    return text[:_STDERR_LIMIT]


def _deadline(budget):
    if budget is None:
        return None
    return time.monotonic() + max(0, float(budget))


def _run(argv, cwd=None, text=False, deadline=None):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    timeout = None if deadline is None else max(0, deadline - time.monotonic())
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=text, env=environment, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process)
        raise ReceiveError("timed out waiting for " + " ".join(argv[:4]),
                           stage="command", reason="timeout")
    completed = subprocess.CompletedProcess(argv, process.returncode, stdout, _redact(stderr))
    return completed


def _stop(process):
    if os.name == "nt":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise ReceiveError("child survived cancellation", stage="command",
                           reason="cancel-unconfirmed", exit_code=process.returncode)


def _regular(path):
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not _link_info(info)
    except OSError:
        return False


def _link_info(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _read_regular(path):
    before = path.lstat()
    if _link_info(before):
        raise ReceiveError("file is a link or reparse point")
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise ReceiveError("HANDOFF.md must be a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return b"".join(chunks), (info.st_dev, info.st_ino, info.st_mode)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _durable_bytes(path, data, mode=0o600):
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)


def _safe_directory(path):
    """Refuse a path whose named components traverse a symlink."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _link_info(current.lstat()) or not current.is_dir():
            return False
    return True


def _git(workspace, *args):
    result = _run(["git", *args], cwd=workspace, text=True)
    if result.returncode:
        raise ReceiveError("git command failed")
    return result.stdout.strip()


def _workspace_root(workspace):
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ReceiveError("workspace is not a real directory")
    root = Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve()
    if workspace.resolve() != root:
        raise ReceiveError("workspace must be its Git root")
    return root


def _binding(root):
    path = root / "rules" / "handoff-receive.json"
    if not path.exists() and not path.is_symlink():
        return None, None
    if not _safe_directory(root) or not _safe_directory(path.parent) or not _regular(path):
        raise ReceiveError("binding is not a regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiveError("invalid binding") from exc
    wanted = {"version", "pair", "repository", "branch", "document", "initial_revision"}
    if not isinstance(value, dict) or set(value) != wanted or type(value.get("version")) is not int or value["version"] != 1:
        raise ReceiveError("invalid binding schema")
    if not isinstance(value["pair"], str) or not _PAIR.fullmatch(value["pair"]):
        raise ReceiveError("invalid pair")
    repo = value["repository"]
    if not isinstance(repo, str) or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+", repo):
        raise ReceiveError("repository must be an HTTPS URL")
    if not isinstance(value["branch"], str) or not value["branch"]:
        raise ReceiveError("invalid branch")
    check = _run(["git", "check-ref-format", "--branch", value["branch"]])
    if check.returncode:
        raise ReceiveError("invalid branch")
    doc = value["document"]
    if not isinstance(doc, str) or "\\" in doc or doc.startswith("/") or not doc.endswith(".md"):
        raise ReceiveError("invalid document path")
    parts = doc.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReceiveError("document path is not canonical")
    if not isinstance(value["initial_revision"], str) or not _OID.fullmatch(value["initial_revision"]):
        raise ReceiveError("invalid initial revision")
    return value, raw


def _fragment(repo, oid, document):
    if not _OID.fullmatch(oid):
        raise ReceiveError("invalid revision")
    verified = _run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", oid + "^{commit}"], text=True)
    if verified.returncode:
        raise ReceiveError("revision is unavailable")
    shown = _run(["git", "-C", str(repo), "show", "--no-textconv", "--format=", oid + ":" + document])
    if shown.returncode:
        raise ReceiveError("shared document is unavailable")
    try:
        body = shown.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiveError("shared document is not UTF-8") from exc
    if ("handoff-receive:" in body or "\x00" in body or re.search(r"(?im)^\s*(```|~~~)", body)
            or re.search(r"(?is)<\s*/?\s*script\b", body)
            or re.search(r"(?im)^\s*(?:command|path|workspace|repository|branch)\s*:", body)):
        raise ReceiveError("unsafe shared document")
    return body


def _command_error(completed, stage):
    detail = completed.stderr if isinstance(completed.stderr, str) else _redact(completed.stderr)
    return ReceiveError(detail or "git exited " + str(completed.returncode),
                        stage=stage, reason="transport", exit_code=completed.returncode)


def _fetch(root, binding, common, deadline=None):
    # Reuse Git's existing objects without changing a ref, FETCH_HEAD, index or
    # checkout. Pin the advertised OID before fetching; a moving branch cannot
    # select different bytes halfway through receipt.
    repository = binding["repository"]
    rewrites = _run(["git", "-C", str(root), "config", "--get-regexp", r"^url\..*\.insteadof$"],
                    text=True, deadline=deadline)
    if rewrites.returncode not in (0, 1):
        raise _command_error(rewrites, "rewrite")
    for row in rewrites.stdout.splitlines():
        parts = row.split(None, 1)
        if len(parts) == 2 and repository.startswith(parts[1]):
            raise ReceiveError("configured URL rewrite changes the fixed source",
                               stage="rewrite", reason="invalid-config")
    prefix = ["git", "-C", str(root), "-c", "protocol.allow=never",
              "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false"]
    ref = "refs/heads/" + binding["branch"]
    advertised = _run(prefix + ["ls-remote", "--exit-code", repository, ref],
                      text=True, deadline=deadline)
    rows = advertised.stdout.splitlines()
    if advertised.returncode == 2 and not rows:
        raise ReceiveError("fixed shared branch is absent", stage="advertise",
                           reason="branch-absent", exit_code=2)
    if advertised.returncode:
        raise _command_error(advertised, "advertise")
    if len(rows) != 1:
        raise ReceiveError("invalid shared branch advertisement", stage="advertise",
                           reason="advertisement-invalid", exit_code=advertised.returncode)
    fields = rows[0].split()
    if len(fields) != 2 or fields[1] != ref or not _OID.fullmatch(fields[0]):
        raise ReceiveError("invalid shared branch advertisement", stage="advertise",
                           reason="advertisement-invalid", exit_code=advertised.returncode)
    incoming = fields[0]
    fetched = _run(prefix + ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                             "--no-auto-maintenance", "--refmap=", repository, incoming],
                   deadline=deadline)
    if fetched.returncode:
        raise _command_error(fetched, "fetch")
    return root, incoming


def _exchange_linux(left, right):
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    # syscall is used because glibc does not consistently expose renameat2.
    machine = os.uname().machine
    numbers = {"x86_64": 316, "aarch64": 276, "riscv64": 276}
    number = numbers.get(machine)
    if number is None:
        raise ReceiveError("atomic exchange unsupported on this Linux architecture")
    result = libc.syscall(number, -100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result:
        raise OSError(ctypes.get_errno(), "renameat2 exchange failed")


def _exchange_windows(left, right, backup):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ReplaceFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_uint32,
                                    ctypes.c_void_p, ctypes.c_void_p)
    kernel.ReplaceFileW.restype = ctypes.c_int
    if not kernel.ReplaceFileW(str(right), str(left), str(backup), 0, None, None):
        raise OSError(ctypes.get_last_error(), "ReplaceFileW failed")


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _pending_path(target, store):
    return store / ('pending-' + hashlib.sha256(str(target).encode('utf-8')).hexdigest() + '.json')


def _recover_pending(target, store):
    """A failed replacement must not become a successful receipt on retry."""
    pending = _pending_path(target, store)
    if not pending.exists():
        return
    raw, _ = _read_regular(pending)
    try:
        record = json.loads(raw)
        candidates = [store / record[key] for key in ('original', 'prepared', 'backup')]
        if any(path.parent != store or path.name != record[key]
               for path, key in zip(candidates, ('original', 'prepared', 'backup'))):
            raise ValueError('invalid recovery path')
        original, _ = _read_regular(candidates[0])
        current, _ = _read_regular(target)
        backup = _read_regular(candidates[2])[0] if candidates[2].exists() else None
        digest = lambda value: hashlib.sha256(value).hexdigest()
        if digest(original) != record['original_digest']:
            raise ValueError('original recovery snapshot changed')
        before_exchange = (current == original and
                           (backup is None or digest(backup) == record['desired_digest']))
        completed = (digest(current) == record['desired_digest'] and backup == original)
        # Restoring the displaced file retains the competing edit; the normal
        # receipt comparison can then retry against that actual local state.
        restored = backup is not None and current == backup
        if not (before_exchange or completed or restored):
            raise ValueError('unresolved replacement; preserve and reconcile the displaced file ' + str(candidates[2]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiveError('pending HANDOFF recovery: ' + str(exc)) from None
    pending.unlink()
    _sync_directory(store)


def _publish(target, desired, original, store, original_identity=None):
    """Publish with exchange and retain the displaced bytes even on a race."""
    # A post-exchange failure must leave the old target somewhere durable.  A
    # metadata-store prepared file makes that true without a second rename.
    if os.stat(target.parent).st_dev != os.stat(store).st_dev:
        return _result("write-refused", error="Git metadata is on another filesystem")
    original_copy = store / ("original-" + uuid.uuid4().hex + ".md")
    prepared = store / ("displaced-" + uuid.uuid4().hex + ".md")
    backup = prepared if os.name != 'nt' else store / ("displaced-" + uuid.uuid4().hex + ".bak")
    pending = _pending_path(target, store)
    try:
        _durable_bytes(original_copy, original)
        mode = os.stat(target, follow_symlinks=False).st_mode & 0o777
        _durable_bytes(prepared, desired, mode)
        _sync_directory(store)
        if not _safe_directory(target.parent) or not _safe_directory(store):
            raise ReceiveError("publication parent changed")
        current, identity = _read_regular(target)
        if current != original or (original_identity is not None and identity != original_identity):
            return _result("concurrent-write", error="target changed before publication", recovery=str(original_copy))
        record = dict(original=original_copy.name, prepared=prepared.name, backup=backup.name,
                      original_digest=hashlib.sha256(original).hexdigest(),
                      desired_digest=hashlib.sha256(desired).hexdigest())
        _durable_bytes(pending, json.dumps(record, sort_keys=True).encode('utf-8'))
        _sync_directory(store)
        if os.name == "posix" and os.uname().sysname == "Linux":
            _exchange_linux(prepared, target)
        elif os.name == "nt":
            _exchange_windows(prepared, target, backup)
        else:
            return _result("write-refused", error="atomic publication unsupported")
        _sync_directory(target.parent)
        _sync_directory(store)
        displaced, displaced_identity = _read_regular(backup)
        now, _ = _read_regular(target)
        if displaced != original or now != desired or displaced_identity[:2] != identity[:2]:
            return _result("concurrent-write", error="target changed during publication", recovery=str(backup), original_recovery=str(original_copy))
        pending.unlink()
        _sync_directory(store)
        return _result("updated", recovery=str(backup), original_recovery=str(original_copy))
    except (OSError, ReceiveError) as exc:
        # ReplaceFile can report a partial operation. Never infer that a failed
        # call left every pathname unchanged, and never delete recovery files.
        return _result("write-failed", error=str(exc), recovery=str(backup),
                       prepared=str(prepared), original_recovery=str(original_copy))


def _lock(path):
    if path.exists() and not _regular(path):
        raise ReceiveError("receiver lock is not a regular file")
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        conflict = isinstance(exc, BlockingIOError) or getattr(exc, "winerror", None) in (33, 36)
        if not conflict and exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise
        raise ReceiveError("receiver is already running", stage="lock", reason="busy")
    return handle


def _error_result(exc):
    if isinstance(exc, ReceiveError):
        return _result("error", error=str(exc), stage=exc.stage, reason=exc.reason,
                       exit=exc.exit_code, applied=False)
    return _result("error", error=str(exc), stage="receive", reason="unknown", exit=None, applied=False)


def receive(workspace, budget=BUDGET_SECONDS):
    """Receive a configured fragment.  Returns a structured status dictionary."""
    try:
        declared = Path(workspace)
        # A workspace without the optional fixed binding is outside this
        # feature, including declared workspaces that are not Git roots.
        binding, binding_bytes = _binding(declared)
        if binding is None:
            return _result("not-configured")
        root = _workspace_root(workspace)
        target = root / "HANDOFF.md"
        if not _regular(target):
            raise ReceiveError("HANDOFF.md must be a regular file")
        common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "handoff-receive"
        common.mkdir(parents=True, exist_ok=True)
        if not _safe_directory(common):
            raise ReceiveError("Git recovery directory traverses a link")
        deadline = _deadline(budget)
        with _lock(common / "receiver.lock"):
            _recover_pending(target, common)
            # Recheck after acquiring the receiver lock; outside editors remain
            # protected by the byte checks around the exchange below.
            binding, locked_binding = _binding(root)
            if binding is None or locked_binding != binding_bytes:
                raise ReceiveError("binding changed while receiving")
            original, _identity = _read_regular(target)
            if target.is_symlink():
                raise ReceiveError("HANDOFF.md is a symlink")
            repo, incoming = _fetch(root, binding, common, deadline)
            try:
                text = original.decode("utf-8")
                all_markers = list(_MARK.finditer(text))
                if (text.count("<!-- handoff-receive:") != len(all_markers)
                        or text.count("<!-- /handoff-receive:") != len(all_markers)):
                    raise ReceiveError("malformed receipt marker")
                if len({item.group(1) for item in all_markers}) != len(all_markers):
                    raise ReceiveError("duplicate receipt marker")
                match = next((item for item in all_markers if item.group(1) == binding["pair"]), None)
                has_receipt = match is not None
                prior = match.group(2) if has_receipt else binding["initial_revision"]
                # The initial revision is a history anchor even after a receipt
                # exists, so a marker from another history cannot authorize data.
                anchored = _run(["git", "-C", str(repo), "merge-base", "--is-ancestor", binding["initial_revision"], prior])
                if anchored.returncode not in (0, 1):
                    raise ReceiveError("could not verify initial revision")
                if anchored.returncode == 1:
                    return _result("stale-or-diverged", revision=incoming)
                relation = _run(["git", "-C", str(repo), "merge-base", "--is-ancestor", prior, incoming])
                if relation.returncode not in (0, 1):
                    raise ReceiveError("could not compare revisions")
                if relation.returncode == 1:
                    return _result("stale-or-diverged", revision=incoming)
                prior_body = _fragment(repo, prior, binding["document"]) if has_receipt else None
                incoming_body = _fragment(repo, incoming, binding["document"])
                if prior_body is not None and not prior_body.endswith("\n"):
                    prior_body += "\n"
                if not incoming_body.endswith("\n"):
                    incoming_body += "\n"
                local_body = match.group(3) if has_receipt else None
                # A repeated payload is intentionally a no-op, including when
                # its Git revision changed; otherwise two receivers can echo
                # acknowledgement-only receipt updates forever.
                if has_receipt and incoming_body == prior_body:
                    if _binding(root)[1] != binding_bytes or _read_regular(target) != (original, _identity):
                        raise ReceiveError("inputs changed during receipt")
                    return _result("unchanged", revision=incoming, local_changes=local_body != prior_body)
                if local_body is not None and local_body != prior_body and incoming_body != local_body:
                    return _result("conflict", revision=incoming)
                block = "<!-- handoff-receive:%s revision=%s -->\n%s<!-- /handoff-receive:%s -->" % (
                    binding["pair"], incoming, incoming_body, binding["pair"])
                if has_receipt:
                    desired_text = text[:match.start()] + block + text[match.end():]
                else:
                    heading = re.search(r"(?m)^## (?:現在地|Current state)[ \t]*\r?$", text)
                    if heading:
                        end = text.find("\n", heading.end())
                        end = len(text) if end < 0 else end + 1
                        desired_text = text[:end] + block + "\n" + text[end:]
                    else:
                        desired_text = text + ("" if text.endswith("\n") else "\n") + block + "\n"
                # Recheck all mutable inputs immediately before publication.
                latest_body = _fragment(repo, incoming, binding["document"])
                if not latest_body.endswith("\n"):
                    latest_body += "\n"
                if _binding(root)[1] != binding_bytes or latest_body != incoming_body:
                    raise ReceiveError("source changed while receiving")
                outcome = _publish(target, desired_text.encode("utf-8"), original, common, _identity)
                outcome.setdefault("revision", incoming)
                return outcome
            finally:
                if repo != root:
                    shutil.rmtree(repo, ignore_errors=True)
    except ReceiveError as exc:
        return _error_result(exc)
    except (OSError, UnicodeDecodeError) as exc:
        return _error_result(exc)


def _verify_invoked_source(config_path, workspace):
    settings = json.loads(Path(config_path).read_text(encoding="utf-8"))
    wanted = os.path.normcase(os.path.normpath(workspace))
    matches = [row for row in settings["workspaces"]
               if os.path.normcase(os.path.normpath(row["root"])) == wanted]
    if len(matches) != 1:
        raise ReceiveError("HANDOFF workspace binding is missing or ambiguous",
                           stage="pin", reason="invalid-config")
    binding = matches[0].get("handoff")
    source = Path(__file__).resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    argv = binding.get("argv") if isinstance(binding, dict) else None
    pinned = isinstance(argv, list) and any(Path(arg).resolve() == source for arg in argv if isinstance(arg, str))
    if (not isinstance(binding, dict) or binding.get("owner") != "runtime"
            or binding.get("source_sha256") != digest or not pinned):
        raise ReceiveError("fixed HANDOFF receiver source hash mismatch",
                           stage="pin", reason="pin-mismatch")


def main(argv=None):
    """Fixed CLI boundary for runtime and existing GUI consumers."""
    import argparse
    parser = argparse.ArgumentParser(description="Receive the explicitly bound shared HANDOFF")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config")
    parser.add_argument("--budget-seconds", type=float, default=BUDGET_SECONDS)
    args = parser.parse_args(argv)
    if args.config:
        try:
            _verify_invoked_source(args.config, args.workspace)
        except (OSError, ReceiveError, ValueError, KeyError, TypeError) as exc:
            result = _error_result(exc if isinstance(exc, ReceiveError) else ReceiveError(str(exc), stage="pin", reason="invalid-config"))
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 1
    result = receive(args.workspace, args.budget_seconds)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"not-configured", "updated", "unchanged"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
