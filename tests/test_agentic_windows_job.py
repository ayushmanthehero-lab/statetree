import importlib.util
import importlib
import pytest


def module():
    assert importlib.util.find_spec('statetree.agentic.windows_job') is not None, 'Windows command containment is missing'
    return importlib.import_module('statetree.agentic.windows_job')


class Fn:
    def __init__(self,value): self.value=value; self.calls=[]
    def __call__(self,*a): self.calls.append(a); return self.value


class Kernel:
    def __init__(self,assign=1):
        self.CreateJobObjectW=Fn(900)
        self.SetInformationJobObject=Fn(1)
        self.GetCurrentProcess=Fn(99)
        self.AssignProcessToJobObject=Fn(assign)
        self.CloseHandle=Fn(1)


def test_job_is_noninheritable_and_terminates_children_when_worker_exits():
    m=module(); k=Kernel()
    assert m.attach_worker_to_job(kernel=k)==900
    assert k.CreateJobObjectW.calls==[(None,None)]
    assert k.SetInformationJobObject.calls[0][1]==9
    ptr=k.SetInformationJobObject.calls[0][2]
    assert ptr._obj.BasicLimitInformation.LimitFlags & 0x2000
    assert k.AssignProcessToJobObject.calls==[(900,99)]
    assert not k.CloseHandle.calls  # Must survive until worker OS exit, not kill the running worker early.


def test_assignment_failure_never_runs_uncontained():
    m=module(); k=Kernel(assign=0)
    with pytest.raises(OSError,match='job'): m.attach_worker_to_job(kernel=k)
    assert k.CloseHandle.calls==[(900,)]
