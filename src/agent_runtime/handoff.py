"""Safely receive one fixed shared handoff fragment into ``HANDOFF.md``.

The binding is deliberately local and boring: this module never follows a
location, command, or repository supplied by the received Markdown.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PAIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MARK = re.compile(r"<!-- handoff-receive:([A-Za-z0-9][A-Za-z0-9._-]*) revision=([0-9a-f]+) -->\n?(.*?)<!-- /handoff-receive:\1 -->", re.S)


class ReceiveError(RuntimeError):
    pass


def _result(status, **values):
    return {"status": status, **values}


def _run(argv, cwd=None, text=False):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=text, check=False, env=environment)


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


def _fetch(root, binding, common):
    # Reuse Git's existing objects without changing a ref, FETCH_HEAD, index or
    # checkout. Pin the advertised OID before fetching; a moving branch cannot
    # select different bytes halfway through receipt.
    repository = binding["repository"]
    rewrites = _run(["git", "-C", str(root), "config", "--get-regexp", r"^url\..*\.insteadof$"], text=True)
    if rewrites.returncode not in (0, 1):
        raise ReceiveError("could not check Git URL rewrites")
    for row in rewrites.stdout.splitlines():
        parts = row.split(None, 1)
        if len(parts) == 2 and repository.startswith(parts[1]):
            raise ReceiveError("configured URL rewrite changes the fixed source")
    prefix = ["git", "-C", str(root), "-c", "protocol.allow=never",
              "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false"]
    ref = "refs/heads/" + binding["branch"]
    advertised = _run(prefix + ["ls-remote", "--exit-code", repository, ref], text=True)
    rows = advertised.stdout.splitlines()
    if advertised.returncode or len(rows) != 1:
        raise ReceiveError("fixed shared branch is unavailable")
    fields = rows[0].split()
    if len(fields) != 2 or fields[1] != ref or not _OID.fullmatch(fields[0]):
        raise ReceiveError("invalid shared branch advertisement")
    incoming = fields[0]
    if _run(prefix + ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                       "--no-auto-maintenance", "--refmap=", repository, incoming]).returncode:
        raise ReceiveError("fetch failed")
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
    except OSError:
        handle.close()
        raise ReceiveError("receiver is already running")
    return handle


def receive(workspace):
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
            repo, incoming = _fetch(root, binding, common)
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
        return _result("error", error=str(exc))
    except (OSError, UnicodeDecodeError) as exc:
        return _result("error", error=str(exc))


def main(argv=None):
    """Fixed CLI boundary for runtime and existing GUI consumers."""
    import argparse
    parser = argparse.ArgumentParser(description="Receive the explicitly bound shared HANDOFF")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    result = receive(args.workspace)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"not-configured", "updated", "unchanged"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
