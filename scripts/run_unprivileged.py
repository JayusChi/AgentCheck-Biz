"""Run Python acceptance with a restricted Windows token (PostgreSQL rejects admin).

The token only removes privileges and disables Administrators/Power Users SIDs.
It does not change accounts, filesystem ACLs, firewall rules or sandbox policy. The caller
must already have permission to run the requested local validation.
"""

import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcheck_biz.persistence.runtime import child_environment


def run(arguments, receipt, timeout):
    if os.name != 'nt':
        return subprocess.call([sys.executable, *arguments], cwd=ROOT)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    security = c.WinDLL('advapi32', use_last_error=True)
    size = c.c_size_t
    class Sid(c.Structure):
        _fields_ = [('sid', w.LPVOID), ('attributes', w.DWORD)]
    class Startup(c.Structure):
        _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR), ('title', w.LPWSTR),
            ('x', w.DWORD), ('y', w.DWORD), ('xsize', w.DWORD), ('ysize', w.DWORD),
            ('xchars', w.DWORD), ('ychars', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
            ('show', w.WORD), ('reserved_size', w.WORD), ('reserved_ptr', c.POINTER(w.BYTE)),
            ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]
    class Process(c.Structure):
        _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]
    class BasicLimits(c.Structure):
        _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
            ('minimum_working_set', size), ('maximum_working_set', size), ('active_processes', w.DWORD),
            ('affinity', size), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    class Limits(c.Structure):
        _fields_ = [('basic', BasicLimits), ('io', IO), ('process_memory', size), ('job_memory', size),
                   ('peak_process_memory', size), ('peak_job_memory', size)]
    def api(dll, name, args, result=w.BOOL):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
        return fn
    current = api(kernel, 'GetCurrentProcess', [], w.HANDLE)()
    close = api(kernel, 'CloseHandle', [w.HANDLE])
    checked = lambda ok: ok or (_ for _ in ()).throw(c.WinError(c.get_last_error()))
    token, restricted = w.HANDLE(), w.HANDLE()
    handles = []
    process = Process()
    job = None
    report = dict(status='STARTING', launcher_pid=os.getpid(), privilege_mode='restricted-token',
                  disabled_groups=['Administrators', 'Power Users'], disabled_max_privileges=True)
    try:
        checked(api(security, 'OpenProcessToken', [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])(current, 0x008B, c.byref(token)))
        handles.append(token)
        sid_buffers = [c.create_string_buffer(68), c.create_string_buffer(68)]
        sid_entries = (Sid * 2)()
        for i, kind in enumerate((26, 29)):
            length = w.DWORD(68)
            checked(api(security, 'CreateWellKnownSid', [c.c_int, w.LPVOID, w.LPVOID, c.POINTER(w.DWORD)])(
                kind, None, sid_buffers[i], c.byref(length)))
            sid_entries[i] = Sid(c.cast(sid_buffers[i], w.LPVOID), 0)
        checked(api(security, 'CreateRestrictedToken', [w.HANDLE, w.DWORD, w.DWORD, c.POINTER(Sid),
            w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, c.POINTER(w.HANDLE)])(
                token, 1, 2, sid_entries, 0, None, 0, None, c.byref(restricted)))
        handles.append(restricted)
        # Like PostgreSQL's AddUserToTokenDacl: after disabling Administrators,
        # explicitly retain the current user in this token's default object ACL.
        # Otherwise DLL initialization / child process creation can fail with
        # STATUS_DLL_INIT_FAILED for elevated callers. No filesystem ACL changes.
        get_info = api(security, 'GetTokenInformation', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)])
        def info(kind):
            needed = w.DWORD()
            get_info(restricted, kind, None, 0, c.byref(needed))
            if not needed.value:
                raise c.WinError(c.get_last_error())
            data = c.create_string_buffer(needed.value)
            checked(get_info(restricted, kind, data, needed, c.byref(needed)))
            return data
        user_info, dacl_info = info(1), info(6)
        user_sid = c.cast(user_info, c.POINTER(Sid)).contents.sid
        old_acl = c.cast(dacl_info, c.POINTER(w.LPVOID)).contents.value
        sizes = (w.DWORD * 3)()
        checked(api(security, 'GetAclInformation', [w.LPVOID, w.LPVOID, w.DWORD, c.c_int])(
            old_acl, sizes, c.sizeof(sizes), 2))
        sid_length = api(security, 'GetLengthSid', [w.LPVOID], w.DWORD)(user_sid)
        acl_size = sizes[1] + 8 + sid_length
        new_acl = c.create_string_buffer(acl_size)
        checked(api(security, 'InitializeAcl', [w.LPVOID, w.DWORD, w.DWORD])(new_acl, acl_size, 2))
        for index in range(sizes[0]):
            ace = w.LPVOID()
            checked(api(security, 'GetAce', [w.LPVOID, w.DWORD, c.POINTER(w.LPVOID)])(old_acl, index, c.byref(ace)))
            ace_size = c.cast(ace.value + 2, c.POINTER(w.WORD)).contents.value
            checked(api(security, 'AddAce', [w.LPVOID, w.DWORD, w.DWORD, w.LPVOID, w.DWORD])(
                new_acl, 2, 0xffffffff, ace, ace_size))
        checked(api(security, 'AddAccessAllowedAceEx', [w.LPVOID, w.DWORD, w.DWORD, w.DWORD, w.LPVOID])(
            new_acl, 2, 1, 0x10000000, user_sid))
        default_acl = w.LPVOID(c.addressof(new_acl))
        checked(api(security, 'SetTokenInformation', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD])(
            restricted, 6, c.byref(default_acl), c.sizeof(default_acl)))
        startup = Startup(cb=c.sizeof(Startup), flags=0x101, show=0)
        for field, number in (('stdin', -10), ('stdout', -11), ('stderr', -12)):
            source = api(kernel, 'GetStdHandle', [w.DWORD], w.HANDLE)(number & 0xffffffff)
            duplicate = w.HANDLE()
            checked(api(kernel, 'DuplicateHandle', [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD])(
                current, source, current, c.byref(duplicate), 0, True, 2))
            handles.append(duplicate)
            setattr(startup, field, duplicate)
        job = api(kernel, 'CreateJobObjectW', [w.LPVOID, w.LPCWSTR], w.HANDLE)(None, None)
        checked(job)
        handles.append(job)
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(api(kernel, 'SetInformationJobObject', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD])(
            job, 9, c.byref(limits), c.sizeof(limits)))
        executable = sys._base_executable
        command = c.create_unicode_buffer(subprocess.list2cmdline([executable, '-X', 'utf8', *arguments]))
        env = child_environment(dict(PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                                     __PYVENV_LAUNCHER__=sys.executable))
        environment = c.create_unicode_buffer('\0'.join(k + '=' + v for k, v in sorted(env.items())) + '\0\0')
        checked(api(security, 'CreateProcessAsUserW', [w.HANDLE, w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID,
            w.BOOL, w.DWORD, w.LPVOID, w.LPCWSTR, c.POINTER(Startup), c.POINTER(Process)])(
                restricted, executable, command, None, None, True, 0x08000404, environment,
                str(ROOT), c.byref(startup), c.byref(process)))
        handles.extend([process.process, process.thread])
        checked(api(kernel, 'AssignProcessToJobObject', [w.HANDLE, w.HANDLE])(job, process.process))
        resume = api(kernel, 'ResumeThread', [w.HANDLE], w.DWORD)(process.thread)
        if resume == 0xffffffff:
            raise c.WinError(c.get_last_error())
        report.update(status='RUNNING', pid=process.pid)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(report, indent=2), encoding='utf8')
        wait = api(kernel, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
        started = time.monotonic()
        while True:
            waited = wait(process.process, 1000)
            if waited == 0:
                break
            if waited != 0x102:
                raise c.WinError(c.get_last_error())
            if time.monotonic() - started >= timeout:
                raise TimeoutError('Acceptance process exceeded launcher deadline')
        code = w.DWORD()
        checked(api(kernel, 'GetExitCodeProcess', [w.HANDLE, c.POINTER(w.DWORD)])(process.process, c.byref(code)))
        report.update(status='PASS' if code.value == 0 else 'ERROR', exit_code=code.value, exited=True)
        return code.value
    finally:
        # Job handle closes last: outstanding descendants, if any, cannot leak.
        if process.process and report['status'] not in {'PASS', 'ERROR'}:
            api(kernel, 'TerminateProcess', [w.HANDLE, w.UINT])(process.process, 1)
            report.update(status='ERROR', reason='Launcher did not observe normal completion')
        for handle in reversed(handles):
            if handle != job:
                close(handle)
        if job:
            close(job)
        report['job_closed'] = job is not None
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(report, indent=2), encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    if not arguments:
        parser.error('A Python script or -m module is required')
    return run(arguments, args.receipt.resolve(), args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
