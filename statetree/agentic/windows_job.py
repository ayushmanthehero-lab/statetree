"""Kill-on-close Windows job owned by the independent command worker.

The worker joins before spawning user code. Its non-inherited job handle lives
until process exit; Windows then terminates any remaining descendants. This is
process-lifetime containment, NOT a filesystem/network/privilege sandbox.
"""
import ctypes as C
import os


class BasicLimits(C.Structure):
    _fields_=[('PerProcessUserTimeLimit',C.c_int64),('PerJobUserTimeLimit',C.c_int64),
              ('LimitFlags',C.c_uint32),('MinimumWorkingSetSize',C.c_size_t),
              ('MaximumWorkingSetSize',C.c_size_t),('ActiveProcessLimit',C.c_uint32),
              ('Affinity',C.c_size_t),('PriorityClass',C.c_uint32),('SchedulingClass',C.c_uint32)]


class IOCounters(C.Structure):
    _fields_=[(name,C.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                                           'ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class ExtendedLimits(C.Structure):
    _fields_=[('BasicLimitInformation',BasicLimits),('IoInfo',IOCounters),
              ('ProcessMemoryLimit',C.c_size_t),('JobMemoryLimit',C.c_size_t),
              ('PeakProcessMemoryUsed',C.c_size_t),('PeakJobMemoryUsed',C.c_size_t)]


_job_handle=None


def attach_worker_to_job(*,kernel=None):
    global _job_handle
    if kernel is None:
        if os.name!='nt': return None
        kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[C.c_void_p,C.c_wchar_p]; kernel.CreateJobObjectW.restype=C.c_void_p
    kernel.SetInformationJobObject.argtypes=[C.c_void_p,C.c_int,C.c_void_p,C.c_uint32]; kernel.SetInformationJobObject.restype=C.c_int
    kernel.AssignProcessToJobObject.argtypes=[C.c_void_p,C.c_void_p]; kernel.AssignProcessToJobObject.restype=C.c_int
    kernel.GetCurrentProcess.argtypes=[]; kernel.GetCurrentProcess.restype=C.c_void_p
    kernel.CloseHandle.argtypes=[C.c_void_p]; kernel.CloseHandle.restype=C.c_int
    job=kernel.CreateJobObjectW(None,None)  # non-inheritable handle
    if not job: raise OSError('Could not create Windows command-worker job')
    limits=ExtendedLimits(); limits.BasicLimitInformation.LimitFlags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not kernel.SetInformationJobObject(job,9,C.byref(limits),C.sizeof(limits)):
            raise OSError('Could not set Windows job kill-on-close limits')
        if not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):
            raise OSError('Could not attach command worker to Windows job; no command was run')
    except BaseException:
        kernel.CloseHandle(job)
        raise
    _job_handle=job  # Do NOT close this while the worker is recording its receipt.
    return job
