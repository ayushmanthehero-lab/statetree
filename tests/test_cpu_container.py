"""Offline checks for the CPU image entrypoint; never launches llama.cpp or AWS."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import unittest


ENTRYPOINT = Path(__file__).parents[1] / "deploy" / "llama_cpp_cpu" / "serve.py"
spec = importlib.util.spec_from_file_location("cpu_serve", ENTRYPOINT)
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


def test_default_command_uses_baked_model_and_cpu_limits():
    command = serve.server_command({})
    options = dict(zip(command[1::2], command[2::2]))
    assert options["--model"] == "/opt/statetree/model/Qwen3.5-4B-Q4_K_M.gguf"
    assert options["--host"] == "127.0.0.1"
    assert options["--port"] == "8081"
    assert options["--threads"] == "4"
    assert options["--threads-batch"] == "4"
    assert options["--ctx-size"] == "4096"
    assert options["--parallel"] == "1"
    assert options["--n-gpu-layers"] == "0"
    assert json.loads(options["--chat-template-kwargs"]) == {"enable_thinking": False}
    assert command[-1] == "--jinja"


def test_environment_values_remain_single_arguments_without_shell_parsing():
    command = serve.server_command({
        "SM_LLAMA_CPP_ALIAS": "Qwen/Qwen3.5-4B",
        "SM_LLAMA_CPP_CTX_SIZE": "2048",
        "SM_LLAMA_CPP_CHAT_TEMPLATE_KWARGS": '{"enable_thinking":false,"note":"a b; echo hi"}',
    })
    assert command[command.index("--alias") + 1] == "Qwen/Qwen3.5-4B"
    assert command[command.index("--ctx-size") + 1] == "2048"
    assert json.loads(command[command.index("--chat-template-kwargs") + 1])["note"] == "a b; echo hi"


def test_bad_environment_fails_before_starting_children():
    for environment in [
        {"SM_LLAMA_CPP_THREADS": "0"},
        {"SM_LLAMA_CPP_THREADS": "4 --host 0.0.0.0"},
        {"SM_LLAMA_CPP_CHAT_TEMPLATE_KWARGS": "not-json"},
        {"SM_LLAMA_CPP_CHAT_TEMPLATE_KWARGS": "[]"},
        {"SM_LLAMA_CPP_JINJA": "sometimes"},
        {"SM_LLAMA_CPP_HF_REPO": "unexpected/download"},
    ]:
        with unittest.TestCase().assertRaises(ValueError):
            serve.server_command(environment)


def test_model_server_failure_stops_proxy_and_propagates_failure():
    server, proxy = Mock(), Mock()
    server.poll.return_value = 3
    proxy.poll.return_value = None
    with patch.object(serve.subprocess, "Popen", side_effect=[server, proxy]):
        assert serve.supervise([["llama-server"], ["nginx"]]) == 3
    proxy.terminate.assert_called_once()
    proxy.wait.assert_called()


def test_proxy_start_failure_stops_already_running_server():
    server = Mock()
    server.poll.return_value = None
    with patch.object(serve.subprocess, "Popen", side_effect=[server, OSError("missing nginx")]):
        with unittest.TestCase().assertRaisesRegex(OSError, "missing nginx"):
            serve.supervise([["llama-server"], ["nginx"]])
    server.terminate.assert_called_once()


def test_sigterm_stops_both_children():
    server, proxy = Mock(), Mock()
    server.poll.return_value = proxy.poll.return_value = None
    with patch.object(serve.subprocess, "Popen", side_effect=[server, proxy]), \
         patch.object(serve.time, "sleep", side_effect=lambda _: serve.request_shutdown(None, None)):
        assert serve.supervise([["llama-server"], ["nginx"]]) == 0
    server.terminate.assert_called_once()
    proxy.terminate.assert_called_once()


def test_stuck_child_is_killed_after_grace_period():
    child = Mock()
    child.poll.return_value = None
    child.wait.side_effect = [serve.subprocess.TimeoutExpired("server", 10), 0]
    serve.stop_children([child])
    child.terminate.assert_called_once()
    child.kill.assert_called_once()


def test_shutdown_received_before_start_does_not_launch_children():
    serve.request_shutdown(None, None)
    with patch.object(serve.subprocess, "Popen") as launch:
        assert serve.supervise([["llama-server"], ["nginx"]]) == 0
    launch.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(
                                  value, setUp=lambda: setattr(serve, "_shutdown", False))
                              for name, value in globals().items()
                              if name.startswith("test_") and callable(value))
