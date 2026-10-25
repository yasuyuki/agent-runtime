"""Native CLI adapter; configuration is explicit and other owners stay external."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import stat
import sys
import sysconfig

VERSION = 1
USAGE = """usage: agent-runtime [--config PATH] [--handoff-independent] [--explain] TOOL [-- VENDOR-ARGS...]

Launch TOOL's configured vendor from the current directory. Inside a configured
workspace the launch goes through that workspace's pinned lifecycle. Anywhere
else the vendor runs directly and a notice goes to stderr. A refusal is one
JSON object on stderr with "error" and, when one exists, "recovery".

  --config PATH            version-1 configuration (default: AGENT_RUNTIME_CONFIG)
  --handoff-independent    launch without the HANDOFF receipt; a notice stays on stderr
  --explain                print the resolved launch as JSON on stdout and run nothing:
                           vendor, effective cwd, workspace, verified lifecycle argv,
                           HANDOFF route, final command and notices. Agents call the
                           lifecycle argv for status, leases, holds and finish.
                           Exit 0 when the launch would start, 2 with the refusal."""
DEPTH_ENV = 'AGENT_RUNTIME_DEPTH'


class RuntimeError_(RuntimeError):
    def __init__(self, message, recovery=None):
        super().__init__(message)
        self.recovery = recovery


def _error(exc, stream=None):
    payload = {'ok': False, 'error': str(exc)}
    if getattr(exc, 'recovery', None):
        payload['recovery'] = exc.recovery
    print(json.dumps(payload, ensure_ascii=False), file=stream or sys.stderr)
    return 2


def _absolute(value, label='path'):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError_(label + ' must be an absolute path')
    return Path(value).resolve()


_FIX_CONFIG = 'fix the configuration file named by --config or AGENT_RUNTIME_CONFIG'


def _read_config(value, *, repair=False):
    path = value or os.environ.get('AGENT_RUNTIME_CONFIG')
    if not path:
        raise RuntimeError_('--config or AGENT_RUNTIME_CONFIG is required')
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('version') != VERSION:
        raise RuntimeError_('unsupported runtime configuration version')
    if not isinstance(raw.get('tools'), dict):
        raise RuntimeError_('runtime configuration requires tools')
    if not repair and not isinstance(raw.setdefault('workspaces', []), list):
        raise RuntimeError_('runtime configuration workspaces must be a list', _FIX_CONFIG)
    return raw


def _argv(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise RuntimeError_(label + ' must be a nonempty string array')
    _absolute(value[0], label + ' executable')
    return list(value)


def _vendor_argv(value):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise RuntimeError_('vendor must be a nonempty string array')
    if not Path(value[0]).is_absolute():
        raise RuntimeError_('vendor executable must be an absolute path')
    return list(value)


def _vendor_path_error(path, exc):
    if isinstance(exc, FileNotFoundError):
        return RuntimeError_('vendor executable is unavailable: ' + str(path))
    return exc


def _windows_vendor_path(path):
    current = Path(path.anchor)
    pending = list(path.parts[1:])
    seen = set()
    junction = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            if current.parent == current:
                raise RuntimeError_('vendor executable path leaves its volume')
            current = current.parent
            continue
        candidate = current / part
        try:
            metadata = os.lstat(candidate)
        except OSError as exc:
            raise _vendor_path_error(candidate, exc) from exc
        tag = getattr(metadata, 'st_reparse_tag', None)
        symlink = stat.S_ISLNK(metadata.st_mode)
        is_junction = tag == junction
        if tag and not (symlink or is_junction):
            raise RuntimeError_('vendor executable path has unsupported reparse point: ' + str(candidate))
        if not (symlink or is_junction):
            current = candidate
            continue
        key = os.path.normcase(os.path.normpath(os.fspath(candidate)))
        if key in seen:
            raise RuntimeError_('vendor executable path has a link cycle: ' + str(candidate))
        seen.add(key)
        try:
            target = os.readlink(candidate)
        except OSError as exc:
            raise _vendor_path_error(candidate, exc) from exc
        namespaced = target.startswith('\\??\\') or target.startswith('\\\\?\\')
        if namespaced:
            target = target[4:]
        target_path = Path(target)
        if namespaced and not target_path.is_absolute():
            raise RuntimeError_('vendor executable path has unsupported link target: ' + str(candidate))
        if target_path.is_absolute():
            current = Path(target_path.anchor)
            pending = list(target_path.parts[1:]) + pending
        else:
            current = candidate.parent
            pending = list(target_path.parts) + pending
    return current


def _vendor_executable(value):
    path = Path(value[0])
    try:
        return path.resolve(), None
    except OSError as exc:
        if os.name != 'nt' or getattr(exc, 'winerror', None) != 448:
            raise
        return _windows_vendor_path(path), list(value)


# Only native options whose operands can look like directory flags need skipping.
_VALUES = {
    'codex': {'-c', '--config', '-m', '--model', '-p', '--profile', '-s', '--sandbox',
              '-a', '--ask-for-approval', '-i', '--image', '--output-schema', '-o',
              '--output-last-message', '--enable', '--disable', '--add-dir', '--remote',
              '--remote-auth-token-env', '--local-provider'},
    'grok': {'--agent', '--agents', '--allow', '--allowedTools', '--debug-file', '--deny',
             '--disallowedTools', '--disallowed-tools', '--json-schema', '--leader-socket',
             '-m', '--model', '--max-turns', '--output-format', '-p', '--prompt', '--single',
             '--permission-mode', '--prompt-file', '--prompt-json', '--reasoning-effort',
             '--effort', '--rules', '-s', '--session-id', '--sandbox', '--system-prompt',
             '--system-prompt-override', '--tools', '--worktree-ref', '--ref'},
}


def _native_cwd(tool, argv, launch):
    options = {'codex': ('--cd', '-C'), 'grok': ('--cwd',)}.get(tool, ())
    found = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        if arg in _VALUES.get(tool, ()):
            index += 2
            continue
        if arg in options:
            index += 1
            if index == len(argv):
                raise RuntimeError_(arg + ' requires a directory')
            found.append(argv[index])
        else:
            for option in options:
                if arg.startswith(option + '='):
                    found.append(arg[len(option) + 1:])
                elif option == '-C' and arg.startswith('-C') and len(arg) > 2:
                    found.append(arg[2:])
        index += 1
    if len(found) > 1:
        raise RuntimeError_('multiple native cwd overrides are ambiguous')
    if found and not found[0]:
        raise RuntimeError_('native cwd override is empty')
    cwd = (launch / found[0]).resolve() if found else launch
    if not cwd.is_dir():
        raise RuntimeError_('native cwd does not exist: ' + str(cwd))
    # An explicit native cwd prevents resume from selecting historical scope.
    native = list(argv) if found or not options else [options[0], str(cwd), *argv]
    return cwd, native


def _workspace(config, cwd):
    """Select the lifecycle binding for cwd, or None outside every root.

    A workspace binds a pinned lifecycle to a subtree. It does not list the
    places or tools a user may launch.
    """
    matches = []
    for item in config['workspaces']:
        if not isinstance(item, dict):
            raise RuntimeError_('invalid workspace configuration', _FIX_CONFIG)
        root = _absolute(item.get('root'), 'workspace root')
        if cwd == root or root in cwd.parents:
            matches.append((len(root.parts), root, item))
    if not matches:
        return None
    depth = max(size for size, _, _ in matches)
    matches = [(root, item) for size, root, item in matches if size == depth]
    if len(matches) != 1:
        # Equal roots name two pinned lifecycles; picking one would run code
        # the configuration did not select.
        raise RuntimeError_('configuration binds workspace root more than once: ' + str(matches[0][0]),
                            'remove the duplicate workspace root from the runtime configuration')
    root, item = matches[0]
    return {**item, '_root': str(root)}


def _maintenance_for(tool, argv):
    if argv in (['--help'], ['-h'], ['--version']):
        return True
    forms = {
        'codex': (['-V'], ['help'], ['login'], ['login', 'status'], ['logout'], ['update'], ['doctor']),
        'claude': (['-v'], ['update'], ['upgrade'], ['doctor'], ['auth', 'login'],
                   ['auth', 'logout'], ['auth', 'status']),
        'grok': (['-v'], ['help'], ['login'], ['logout'], ['update'],
                 ['doctor'], ['inspect'], ['inspect', '--json'], ['version'], ['v']),
    }
    return argv in forms.get(tool, ())


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dependency(spec, label):
    if not isinstance(spec, dict):
        raise RuntimeError_(label + ' interface is required')
    command = _argv(spec.get('argv'), label + ' argv')
    source = _absolute(spec.get('source'), label + ' source')
    if not any(Path(arg).is_absolute() and Path(arg).resolve() == source for arg in command):
        raise RuntimeError_(label + ' source must be an invoked absolute argv element')
    pins = spec.get('pins')
    if label == 'handoff':
        pins = [{'path': str(source), 'sha256': spec.get('source_sha256')}]
    if not isinstance(pins, list) or not pins:
        raise RuntimeError_(label + ' requires nonempty source pins')
    covered = False
    for pin in pins:
        if not isinstance(pin, dict) or not isinstance(pin.get('sha256'), str):
            raise RuntimeError_('invalid ' + label + ' source pin')
        path = _absolute(pin.get('path'), label + ' pin')
        if _sha256(path) != pin['sha256'].lower():
            raise RuntimeError_(label + ' source pin does not match: ' + str(path),
                                'the configuration owner reviews the changed file and updates its pin; '
                                'the runtime never runs an unpinned ' + label)
        covered |= path == source
    if not covered:
        raise RuntimeError_(label + ' invoked source is not pinned')
    return command


def _handoff(workspace):
    spec = workspace.get('handoff')
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise RuntimeError_('invalid handoff configuration', _FIX_CONFIG)
    if spec.get('owner') == 'external':
        if spec.get('attested') is not True:
            raise RuntimeError_('external handoff owner lacks explicit attestation', _FIX_CONFIG)
        return {'owner': 'external'}
    if spec.get('owner') != 'runtime':
        raise RuntimeError_('handoff requires owner runtime or external', _FIX_CONFIG)
    return {'owner': 'runtime', 'argv': _dependency(spec, 'handoff')}


_INDEPENDENT = 'for work that does not need the HANDOFF, rerun as agent-runtime --handoff-independent TOOL'


def _receive(command, root):
    from agent_runtime.handoff import BUDGET_SECONDS
    try:
        result = subprocess.run([*command, '--workspace', root,
                                 '--budget-seconds', str(BUDGET_SECONDS)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', timeout=BUDGET_SECONDS,
                                env={**os.environ, DEPTH_ENV: '1'})
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError_('handoff receiver failed with reason timeout stage receive applied false',
                            _INDEPENDENT) from exc
    receipt = {}
    if result.stdout:
        try:
            receipt = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError_('handoff receiver failed with exit ' + str(result.returncode),
                                _INDEPENDENT) from exc
    if result.returncode or not isinstance(receipt, dict) or receipt.get('status') not in {'updated', 'unchanged', 'not-configured'}:
        reason = receipt.get('reason', 'unknown') if isinstance(receipt, dict) else 'unknown'
        stage = receipt.get('stage', 'receive') if isinstance(receipt, dict) else 'receive'
        raise RuntimeError_('handoff receiver failed with exit ' + str(result.returncode)
                            + ' reason ' + str(reason) + ' stage ' + str(stage), _INDEPENDENT)


def _launch(command, env):
    if os.name == 'posix':
        os.execvpe(command[0], command, env)
    # Windows delivers console Ctrl+C to the inherited foreground group. Wait
    # for the child owner to finish its existing job/lease cleanup.
    # CTRL_BREAK is delivered to the inherited group as well as its child.
    # Leave forwarding/descendant ownership with lifecycle; this thin parent
    # must survive long enough to return the child's result.
    signal.signal(signal.SIGBREAK, lambda *_: None)
    process = subprocess.Popen(command, env=env)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue


def _plan(tool, argv, config_path, independent=False):
    """Resolve one launch without running the vendor, lifecycle or receiver."""
    if os.environ.get(DEPTH_ENV):
        raise RuntimeError_('agent-runtime recursion refused')
    repair = _maintenance_for(tool, argv)
    config = _read_config(config_path, repair=repair)
    spec = config['tools'].get(tool)
    if not isinstance(spec, dict):
        raise RuntimeError_('unknown tool: ' + tool,
                            'add an absolute vendor argv for this tool to the runtime configuration')
    vendor = _vendor_argv(spec.get('argv'))
    executable, configured_vendor = _vendor_executable(vendor)
    if configured_vendor is not None:
        vendor[0] = str(executable)
    scripts = Path(sysconfig.get_path('scripts'))
    suffix = '.exe' if os.name == 'nt' else ''
    managed = {Path(sys.argv[0]).resolve(), Path(__file__).resolve()}
    managed.update((scripts / (name + suffix)).resolve()
                   for name in ('agent-runtime', 'codex', 'claude', 'grok'))
    if (executable in managed
            or any(vendor[i:i + 2] in (['-m', 'agent_runtime'], ['-m', 'agent_runtime.cli'])
                   for i in range(len(vendor)))):
        raise RuntimeError_('vendor executable recurses to agent-runtime')
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError_('vendor executable is unavailable: ' + str(executable))
    plan = {'tool': tool, 'vendor': vendor, 'maintenance': repair, 'workspace': None,
            'lifecycle': None, 'handoff': None, 'notices': []}
    if configured_vendor is not None:
        plan['configured_vendor'] = configured_vendor
    if repair:
        return {**plan, 'command': [*vendor, *argv]}
    launch = Path.cwd().resolve()
    effective, native = _native_cwd(tool, argv, launch)
    plan.update(launch_cwd=str(launch), effective_cwd=str(effective))
    workspace = _workspace(config, effective)
    handoff = _handoff(workspace) if workspace else None
    if handoff and handoff['owner'] == 'runtime' and independent:
        handoff['skipped'] = 'explicit-independent'
    plan.update(workspace=workspace and workspace['_root'], handoff=handoff)
    lifecycle = workspace.get('lifecycle') if workspace else None
    if lifecycle is None:
        plan['notices'].append({'lifecycle': 'none', 'cwd': str(effective),
                                'reason': 'no configured workspace binds a lifecycle here; '
                                          'the vendor runs without lifecycle supervision'})
        return {**plan, 'command': [*vendor, *native]}
    if not isinstance(lifecycle, dict) or lifecycle.get('interface') != 'resolve-run-v1':
        raise RuntimeError_('unsupported lifecycle interface',
                            'bind a resolve-run-v1 lifecycle to ' + workspace['_root'])
    command = _dependency(lifecycle, 'lifecycle')
    plan['lifecycle'] = command
    return {**plan, 'command': [*command, 'resolve-run', '--cwd', str(effective),
                                '--launch-cwd', str(launch), '--', *vendor, *native]}


def _run(tool, argv, config_path, independent=False):
    plan = _plan(tool, argv, config_path, independent)
    handoff = plan['handoff']
    if handoff and handoff.get('skipped'):
        print(json.dumps({'handoff': 'skipped', 'independent': True, 'reason': 'explicit-independent'},
                         ensure_ascii=False), file=sys.stderr)
    elif handoff and handoff['owner'] == 'runtime':
        _receive(handoff['argv'], plan['workspace'])
    for notice in plan['notices']:
        print(json.dumps({'notice': notice}, ensure_ascii=False), file=sys.stderr)
    # A real vendor may legitimately launch another managed CLI. Recursion
    # guards for receiver callbacks must not prohibit those descendants.
    return _launch(plan['command'], os.environ.copy())


def _explain(tool, argv, config_path, independent):
    try:
        plan = _plan(tool, argv, config_path, independent)
    except (RuntimeError_, OSError, ValueError) as exc:
        return _error(exc, sys.stdout)
    print(json.dumps({'ok': True, **plan}, ensure_ascii=False))
    return 0


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    config = None
    independent = explain = False
    while values and values[0] in {'--config', '--handoff-independent', '--explain'}:
        option = values.pop(0)
        if option == '--config':
            if not values:
                return _error('--config requires a path')
            config = values.pop(0)
        elif option == '--explain':
            explain = True
        else:
            independent = True
    if values == ['--version']:
        from agent_runtime import __version__
        print(__version__)
        return 0
    if not values or values[0] in {'--help', '-h'}:
        print(USAGE)
        return 0
    tool = values.pop(0)
    if values[:1] == ['--']:
        values.pop(0)
    if explain:
        return _explain(tool, values, config, independent)
    try:
        return _run(tool, values, config, independent)
    except (RuntimeError_, OSError, ValueError) as exc:
        return _error(exc)


def _tool_main(tool):
    try:
        return _run(tool, sys.argv[1:], None)
    except (RuntimeError_, OSError, ValueError) as exc:
        return _error(exc)


def codex_main(): return _tool_main('codex')
def claude_main(): return _tool_main('claude')
def grok_main(): return _tool_main('grok')


if __name__ == '__main__':
    raise SystemExit(main())
