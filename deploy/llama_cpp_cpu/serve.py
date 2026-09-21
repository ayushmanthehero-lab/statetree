"""SageMaker CPU entrypoint: supervise llama-server and the nginx adapter."""

import json
import os
import signal
import subprocess
import sys
import time


MODEL = "/opt/statetree/model/Qwen3.5-4B-Q4_K_M.gguf"
DEFAULTS = {
    "ALIAS": "Qwen/Qwen3.5-4B",
    "CTX_SIZE": "4096",
    "THREADS": "4",
    "THREADS_BATCH": "4",
    "PARALLEL": "1",
    "CHAT_TEMPLATE_KWARGS": '{"enable_thinking":false}',
    "JINJA": "true",
}
_shutdown = False


def server_command(environment):
    """Translate the documented CPU profile without invoking a shell."""
    prefix = "SM_LLAMA_CPP_"
    supplied = {key[len(prefix):]: value for key, value in environment.items()
                if key.startswith(prefix)}
    unknown = supplied.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError("Unsupported CPU image settings: " + ", ".join(sorted(unknown)))
    settings = {**DEFAULTS, **supplied}
    for name in ("CTX_SIZE", "THREADS", "THREADS_BATCH", "PARALLEL"):
        value = settings[name]
        if not value.isascii() or not value.isdigit() or int(value) < 1:
            raise ValueError(name + " must be a positive integer")
    if not isinstance(json.loads(settings["CHAT_TEMPLATE_KWARGS"]), dict):
        raise ValueError("CHAT_TEMPLATE_KWARGS must be a JSON object")
    jinja = settings.pop("JINJA").lower()
    if jinja not in ("true", "false"):
        raise ValueError("JINJA must be true or false")
    command = ["llama-server", "--host", "127.0.0.1", "--port", "8081",
               "--model", MODEL, "--n-gpu-layers", "0"]
    for name, value in settings.items():
        command.extend(["--" + name.lower().replace("_", "-"), value])
    command.append("--jinja" if jinja == "true" else "--no-jinja")
    return command


def request_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(commands):
    """Exit if either service dies, and forward shutdown to both children."""
    children = []
    try:
        for command in commands:
            if _shutdown:
                return 0
            children.append(subprocess.Popen(command))
        while not _shutdown:
            for child in children:
                code = child.poll()
                if code is not None:
                    print("Serving process exited: " + str(code), file=sys.stderr, flush=True)
                    return code if code > 0 else 1
            time.sleep(0.2)
        return 0
    finally:
        stop_children(children)


def main():
    if sys.argv[1:] not in ([], ["serve"]):
        raise ValueError("Only the SageMaker 'serve' command is supported")
    command = server_command(os.environ)
    if not os.path.isfile(MODEL):
        raise FileNotFoundError("Baked GGUF is missing: " + MODEL)
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return supervise([command, ["nginx", "-c", "/etc/nginx/statetree.conf", "-g", "daemon off;"]])


if __name__ == "__main__":
    sys.exit(main())
