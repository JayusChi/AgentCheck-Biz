"""Each worker joins its own Windows kill-on-close job before allocating services."""
import ctypes as c
from ctypes import wintypes as w
import os


def own_descendants():
    if os.name!='nt':raise RuntimeError('Native execution currently requires Windows')
    kernel=c.WinDLL('kernel32',use_last_error=True)
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
            ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),
            ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(c.Structure):
        _fields_=[(n,c.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Limits(c.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),
            ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    kernel.CreateJobObjectW.argtypes=[w.LPVOID,w.LPCWSTR]
    kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,w.LPVOID,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.GetCurrentProcess.restype=w.HANDLE
    handle=kernel.CreateJobObjectW(None,None)
    limits=Limits()
    limits.basic.flags=0x2000
    if not handle or not kernel.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)):
        raise c.WinError(c.get_last_error())
    if not kernel.AssignProcessToJobObject(handle,kernel.GetCurrentProcess()):
        raise c.WinError(c.get_last_error())
    # Deliberately retain handle until process termination. Closing it inside the
    # worker would terminate this worker as well as all descendants of its job.
    return handle
