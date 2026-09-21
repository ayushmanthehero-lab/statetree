"""Run the explicitly SDK-free test subset. The full suite is `python -m pytest`.

Seven legacy tests instantiate the real SDK indirectly and are intentionally
outside this subset. They are not changed or marked as passing; full-suite
validation still includes them. Optional framework/bridge tests report skips.
"""
from pathlib import Path
import subprocess
import sys

MODULES = [
    'tests/test_local_gpu.py', 'tests/test_project.py', 'tests/test_project_hardening.py', 'tests/test_cli.py',
    'tests/test_workbench.py', 'tests/test_workbench_browser.py', 'tests/test_project_inference.py',
    'tests/test_workspace.py', 'tests/test_branches.py', 'tests/test_parallel.py',
    'tests/test_context.py', 'tests/test_usage.py', 'tests/test_memory.py',
    'tests/test_commit_memory.py', 'tests/test_durable.py', 'tests/test_hybrid_web.py',
    'tests/test_chat_api.py', 'tests/test_worker_chat.py', 'tests/test_cpu_container.py',
    'tests/test_hybrid_deploy.py', 'tests/test_hybrid_template.py',
    'tests/test_sagemaker_deploy.py', 'tests/test_sagemaker_cpu_deploy.py',
    'tests/test_optional_adapters.py',
]
SDK_DEPENDENT = [
    'tests/test_chat_api.py::ChatHTTPTests::test_application_rejects_reused_benchmark_credential_for_private_chat',
    'tests/test_chat_api.py::ChatHTTPTests::test_local_application_keeps_chat_durable_and_legacy_benchmark_available',
    'tests/test_worker_chat.py::WorkerChatTests::test_buffered_events_over_one_batch_flush_after_worker_restart',
    'tests/test_worker_chat.py::WorkerChatTests::test_cancellation_after_local_completion_cannot_leave_task_claimed_forever',
    'tests/test_worker_chat.py::WorkerChatTests::test_checkpoint_is_visible_before_the_runner_finishes',
    'tests/test_worker_chat.py::WorkerChatTests::test_new_worker_resumes_same_task_and_receives_persisted_checkpoint',
    'tests/test_worker_chat.py::WorkerChatTests::test_worker_executes_real_http_model_relay_events_and_owner_apply',
]


def main():
    root = Path(__file__).resolve().parents[1]
    print('Explicit local-core subset; not the full SDK/provider validation suite.', flush=True)
    print('Seven legacy real-SDK cases excluded; use python -m pytest for the full suite.', flush=True)
    return subprocess.call([sys.executable, '-m', 'pytest', *MODULES,
                            *('--deselect=' + item for item in SDK_DEPENDENT), '-q', *sys.argv[1:]], cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
