"""Only explicitly selected source and typed verdict fields may leave the machine."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
from xml.etree import ElementTree as ET

STATES = {'PASS', 'FAIL', 'ERROR', 'INCONCLUSIVE'}
STEPS = {'environment-setup', 'configuration', 'dependencies', 'core-tests', 'frontend-install', 'frontend-build',
         'integration', 'source-bundle'}
SAFE_NAMES = {'summary.json', 'report.md', 'junit.xml'}
ERROR_TYPES = {'AssertionError','AttributeError','FileNotFoundError','ImportError','KeyError',
               'ModuleNotFoundError','OSError','PermissionError','RuntimeError','TimeoutError',
               'TypeError','ValueError','ExceptionGroup','OtherError'}
FORBIDDEN = {'.git', '.env', '.venv', '.cache', 'node_modules', '__pycache__',
             'artifacts', 'output', 'results', 'tmp', '.tmp', 'dist', 'logs'}
SECRET = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def diagnostic(info):
    """Only exception category and allowlisted source locations, never its message."""
    root = Path(__file__).resolve().parents[2]
    allowed = set(json.loads((root/'ci/source-files.json').read_text(encoding='utf8')))
    kind, _, trace = info
    frames = []
    while trace:
        path = Path(trace.tb_frame.f_code.co_filename).resolve()
        if root in path.parents:
            name = path.relative_to(root).as_posix()
            if name in allowed: frames.append(dict(path=name,line=trace.tb_lineno))
        trace = trace.tb_next
    return dict(type=kind.__name__ if kind.__name__ in ERROR_TYPES else 'OtherError',frames=frames[-6:])


def regular(root, relative):
    """Reject links/junctions on every path component, before resolving."""
    root = Path(root).absolute()
    reject_linked_directory(root)
    value = PurePosixPath(relative)
    if value.is_absolute() or not value.parts or any(x in {'.', '..'} or ':' in x or '\\' in x for x in value.parts):
        raise ValueError('Unsafe source path')
    current = root
    for part in value.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked source refused')
    if not current.is_file() or root.resolve() not in current.resolve().parents:
        raise ValueError('Source is not a regular file within root')
    return current


def reject_linked_directory(directory):
    for part in [Path(directory).absolute(), *Path(directory).absolute().parents]:
        if part.exists():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked directory refused')


def source_bundle(root, output):
    root, output = Path(root), Path(output)
    names = json.loads((root / 'ci/source-files.json').read_text(encoding='utf8'))
    if not names or len(names) != len(set(names)):
        raise ValueError('Empty or duplicate source allowlist')
    records, contents = [], {}
    for name in sorted(names):
        parts = PurePosixPath(name).parts
        if any(p in FORBIDDEN or p.startswith('.env') for p in parts):
            raise ValueError('Forbidden source category')
        path = regular(root, name)
        data = path.read_bytes()
        if SECRET.search(data):
            raise ValueError('Credential-shaped source content refused')
        contents[name] = data
        records.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest()))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr('SOURCE-SHA256.json', json.dumps(records, indent=2))
    return dict(files=len(records), sha256=sha(output),
                source_sha256=hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest())


def projection(report):
    """No strings from exceptions, HTTP bodies, paths, environments or raw traces."""
    scope = report.get('scope')
    if scope not in {'all', 'core', 'integration'} or report.get('status') not in STATES:
        raise ValueError('Invalid CI report')
    result = dict(schema_version='agentcheck-ci-summary/1', scope=scope, status=report['status'],
                  model_requests=0, steps=[])
    for row in report.get('steps', []):
        if row.get('name') not in STEPS or row.get('status') not in STATES:
            raise ValueError('Invalid CI step')
        item = {k: row[k] for k in ('name', 'status')}
        for key in ('exit_code', 'tests', 'failures', 'errors', 'skipped'):
            if key in row:
                if type(row[key]) is not int:
                    raise ValueError('Invalid numeric verdict')
                item[key] = row[key]
        if 'diagnostics' in row:
            allowed = set(json.loads((Path(__file__).resolve().parents[2]/'ci/source-files.json').read_text(encoding='utf8')))
            item['diagnostics'] = []
            for entry in row['diagnostics'][:10]:
                if entry.get('type') not in ERROR_TYPES: continue
                frames = [dict(path=f['path'],line=f['line']) for f in entry.get('frames',[])[:6]
                    if f.get('path') in allowed and type(f.get('line')) is int and f['line']>0]
                item['diagnostics'].append(dict(type=entry['type'],frames=frames))
        result['steps'].append(item)
    for key in ('source_sha256', 'bundle_sha256'):
        if key in report:
            if not re.fullmatch('[0-9a-f]{64}', report[key]):
                raise ValueError('Invalid digest')
            result[key] = report[key]
    # Fixed numeric observations explain duplicate regressions without business data.
    observed = report.get('observed', {})
    allowed = {'ticket_count', 'ticket_tool_calls', 'network_planned', 'network_checked',
               'recovery_planned', 'recovery_checked'}
    result['observed'] = {k: v for k, v in observed.items() if k in allowed and type(v) is int}
    return result


def publish(report, directory):
    public = projection(report)
    directory = Path(directory)
    reject_linked_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.name not in SAFE_NAMES or not p.is_file() or p.is_symlink() for p in directory.iterdir()):
        raise ValueError('Publish directory contains non-whitelisted content')
    for name in SAFE_NAMES:
        if (directory / name).exists():
            regular(directory, name)
    (directory / 'summary.json').write_text(json.dumps(public, indent=2)+'\n', encoding='utf8')
    rows = public['steps']
    lines = ['# AgentCheck V2 CI', '', 'Status: '+public['status'], 'Scope: '+public['scope'],
             'Model requests: 0', '', '| Step | Status | Exit |', '|---|---|---|']
    lines += [f"| {r['name']} | {r['status']} | {r.get('exit_code', '')} |" for r in rows]
    lines += ['', 'Observations: '+json.dumps(public['observed'], sort_keys=True),
              '', 'Only typed verdicts are published. Raw local logs, credentials and business payloads are excluded.']
    (directory / 'report.md').write_text('\n'.join(lines)+'\n', encoding='utf8')
    suite = ET.Element('testsuite', name='agentcheck-v2-'+public['scope'], tests=str(len(rows)),
        failures=str(sum(r['status']=='FAIL' for r in rows)), errors=str(sum(r['status']=='ERROR' for r in rows)),
        skipped=str(sum(r['status']=='INCONCLUSIVE' for r in rows)))
    for row in rows:
        case = ET.SubElement(suite, 'testcase', name=row['name'])
        tag = {'FAIL':'failure','ERROR':'error','INCONCLUSIVE':'skipped'}.get(row['status'])
        if tag: ET.SubElement(case, tag, message='CI step '+row['status'])
    ET.ElementTree(suite).write(directory / 'junit.xml', encoding='utf-8', xml_declaration=True)
    return public


def ensure_report(directory, scope):
    """A setup/build failure still gets typed evidence without reading raw CI logs."""
    directory = Path(directory)
    if (directory/'summary.json').is_file():
        regular(directory,'summary.json')
        report = json.loads((directory/'summary.json').read_text(encoding='utf8'))
    else:
        report = dict(scope=scope,status='ERROR',steps=[dict(name='environment-setup',status='ERROR')])
    return publish(report,directory)
