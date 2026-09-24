"""Native CLI adapter; configuration is explicit and other owners stay external."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import sysconfig

VERSION = 1
DEPTH_ENV = 'AGENT_RUNTIME_DEPTH'


class RuntimeError_(RuntimeError):
    pass


def _error(message):
    print(json.dumps({'ok': False, 'error': str(message)}, ensure_ascii=False), file=sys.stderr)
    return 2


def _absolute(value, label='path'):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError_(label + ' must be an absolute path')
    return Path(value).resolve()


def _read_config(value, *, repair=False):
    path = value or os.environ.get('AGENT_RUNTIME_CONFIG')
    if not path:
        raise RuntimeError_('--config or AGENT_RUNTIME_CONFIG is required')
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('version') != VERSION:
        raise RuntimeError_('unsupported runtime configuration version')
    if not isinstance(raw.get('tools'), dict):
        raise RuntimeError_('runtime configuration requires tools')
    if not repair and not isinstance(raw.get('workspaces'), list):
        raise RuntimeError_('runtime configuration requires workspaces')
    return raw


def _argv(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise RuntimeError_(label + ' must be a nonempty string array')
    _absolute(value[0], label + ' executable')
    return list(value)


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


def _workspace(config, cwd, tool):
    matches = []
    for item in config['workspaces']:
        if not isinstance(item, dict):
            raise RuntimeError_('invalid workspace configuration')
        root = _absolute(item.get('root'), 'workspace root')
        if cwd == root or root in cwd.parents:
            matches.append((len(root.parts), root, item))
    if not matches:
        raise RuntimeError_('cwd is not declared by runtime configuration: ' + str(cwd))
    depth = max(size for size, _, _ in matches)
    matches = [(root, item) for size, root, item in matches if size == depth]
    if len(matches) != 1:
        raise RuntimeError_('ambiguous workspace for cwd: ' + str(cwd))
    root, item = matches[0]
    if item.get('state') != 'active':
        raise RuntimeError_('workspace is %s: %s' % (item.get('state', 'unknown'), item.get('reason', 'adoption required')))
    if not isinstance(item.get('tools'), list) or tool not in item['tools']:
        raise RuntimeError_('tool is not enabled for workspace: ' + tool)
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
            raise RuntimeError_(label + ' source pin does not match: ' + str(path))
        covered |= path == source
    if not covered:
        raise RuntimeError_(label + ' invoked source is not pinned')
    return command


def _handoff(workspace, independent=False):
    spec = workspace.get('handoff')
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise RuntimeError_('invalid handoff configuration')
    if spec.get('owner') == 'external':
        if spec.get('attested') is not True:
            raise RuntimeError_('external handoff owner lacks explicit attestation')
        return
    if spec.get('owner') != 'runtime':
        raise RuntimeError_('handoff requires owner runtime or external')
    command = _dependency(spec, 'handoff')
    if independent:
        print(json.dumps({'handoff': 'skipped', 'independent': True, 'reason': 'explicit-independent'},
                         ensure_ascii=False), file=sys.stderr)
        return
    from agent_runtime.handoff import BUDGET_SECONDS
    try:
        result = subprocess.run([*command, '--workspace', workspace['_root'],
                                 '--budget-seconds', str(BUDGET_SECONDS)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', timeout=BUDGET_SECONDS,
                                env={**os.environ, DEPTH_ENV: '1'})
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError_('handoff receiver failed with reason timeout stage receive applied false') from exc
    receipt = {}
    if result.stdout:
        try:
            receipt = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError_('handoff receiver failed with exit ' + str(result.returncode)) from exc
    if result.returncode or not isinstance(receipt, dict) or receipt.get('status') not in {'updated', 'unchanged', 'not-configured'}:
        reason = receipt.get('reason', 'unknown') if isinstance(receipt, dict) else 'unknown'
        stage = receipt.get('stage', 'receive') if isinstance(receipt, dict) else 'receive'
        raise RuntimeError_('handoff receiver failed with exit ' + str(result.returncode)
                            + ' reason ' + str(reason) + ' stage ' + str(stage))


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


def _run(tool, argv, config_path, independent=False):
    if os.environ.get(DEPTH_ENV):
        raise RuntimeError_('agent-runtime recursion refused')
    repair = _maintenance_for(tool, argv)
    config = _read_config(config_path, repair=repair)
    spec = config['tools'].get(tool)
    if not isinstance(spec, dict):
        raise RuntimeError_('unknown tool: ' + tool)
    vendor = _argv(spec.get('argv'), 'vendor')
    executable = Path(vendor[0])
    scripts = Path(sysconfig.get_path('scripts'))
    suffix = '.exe' if os.name == 'nt' else ''
    managed = {Path(sys.argv[0]).resolve(), Path(__file__).resolve()}
    managed.update((scripts / (name + suffix)).resolve()
                   for name in ('agent-runtime', 'codex', 'claude', 'grok'))
    if (executable.resolve() in managed
            or any(vendor[i:i + 2] in (['-m', 'agent_runtime'], ['-m', 'agent_runtime.cli'])
                   for i in range(len(vendor)))):
        raise RuntimeError_('vendor executable recurses to agent-runtime')
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError_('vendor executable is unavailable: ' + str(executable))
    # A real vendor may legitimately launch another managed CLI. Recursion
    # guards for receiver callbacks must not prohibit those descendants.
    env = os.environ.copy()
    if repair:
        return _launch([*vendor, *argv], env)
    launch = Path.cwd().resolve()
    effective, native = _native_cwd(tool, argv, launch)
    workspace = _workspace(config, effective, tool)
    lifecycle = workspace.get('lifecycle')
    if not isinstance(lifecycle, dict) or lifecycle.get('interface') != 'resolve-run-v1':
        raise RuntimeError_('unsupported lifecycle interface')
    command = _dependency(lifecycle, 'lifecycle')
    _handoff(workspace, independent)
    return _launch([*command, 'resolve-run', '--cwd', str(effective),
                    '--launch-cwd', str(launch), '--', *vendor, *native], env)


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    config = None
    independent = False
    while values and values[0] in {'--config', '--handoff-independent'}:
        option = values.pop(0)
        if option == '--config':
            if not values:
                return _error('--config requires a path')
            config = values.pop(0)
        else:
            independent = True
    if values == ['--version']:
        from agent_runtime import __version__
        print(__version__)
        return 0
    if not values or values[0] in {'--help', '-h'}:
        print('usage: agent-runtime [--config PATH] [--handoff-independent] TOOL [-- VENDOR-ARGS...]')
        return 0
    tool = values.pop(0)
    if values[:1] == ['--']:
        values.pop(0)
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
