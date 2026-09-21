# StateTree + Qwen3.5-4B on your GTX 1650

This incremental update adds a Windows CUDA model launcher to the delivered StateTree project. It leaves the existing AWS demo, cloud worker and deployment configuration unchanged.

**What runs where:** your Windows PC runs llama.cpp on its NVIDIA GPU, plus StateTree's local workbench. StateTree connects to the model over authenticated loopback HTTP. This ChatGPT session is not connected to your GPU. The scripts must be run on your PC.

## 1. One-time setup

Extract `statetree-gtx1650.zip`. Open PowerShell inside its `statetree-gtx1650` directory (the folder containing README.md).

Prerequisites: 64-bit Windows, 64-bit Python 3.11 or newer, Git for Windows on PATH, a working NVIDIA driver supporting the CUDA 12.4 runtime, and sufficient free disk space. Your 16 GB system RAM is also used. Leave roughly 8 GB free on the cache drive for weights, binary archives and extraction; this is a conservative provisioning suggestion, not measured peak disk use.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Setup-GTX1650.ps1
```

The script creates the local `.venv`, installs StateTree including its **real** Strands SDK dependency, then downloads and SHA-256-verifies:

- llama.cpp `b11064`, Windows x64 CUDA 12.4 build;
- matching CUDA runtime DLLs;
- Unsloth's `Qwen3.5-4B-Q4_K_M.gguf`, pinned at the revision listed below.

The weights are approximately 2.74 GB; the two binary/runtime ZIPs total approximately 616 MB. These are download sizes, not required VRAM. Setup needs internet; it does not download a vision projector or call a hosted inference provider. Model weights and executable binaries are **not bundled** in the source ZIP.

Downloads are stored outside the source repository:

```text
%LOCALAPPDATA%\StateTree\gtx1650
```

Interrupted transfers keep a `.part` file for resumption. A checksum mismatch is rejected. Correct cached assets are reused. `setup` does not prove that CUDA inference works; `start` performs device and actual layer-offload checks.

The `-ExecutionPolicy Bypass` parameter applies to this PowerShell process only. The scripts do not change the machine execution policy, install drivers, change firewall rules or request administrator access. Respect any organizational policy that prohibits running scripts.

## 2. Start Qwen and StateTree together

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1
```

This launches both services in the foreground. Keep that terminal open. After loading, look for:

```text
GPU offload confirmed: X/Y layers. Other buffers/layers may use system RAM.
StateTree workbench: http://127.0.0.1:8765
Local access token: <a randomly generated token>
```

`X/Y` is an illustration; the actual count comes from your llama.cpp log. It must be positive. The launcher rejects a CPU-only build, absent CUDA devices, zero offloaded layers, invalid GGUF headers and an already-occupied port. It does not kill or reuse another model server.

Open the **workbench** address printed in the terminal, normally `http://127.0.0.1:8765`, and enter its token. Use its Chat tab. The separate model endpoint uses port 8080 and an ephemeral key automatically passed to StateTree; you do not need to copy that key. Running `python -m statetree serve` in an unrelated terminal does not inherit this model key; use the combined launcher.

**Ctrl+C in the launcher terminal stops its owned model server as well.** Force-killing the parent process or closing Windows abnormally can leave a child process; restart then reports a busy port rather than terminating arbitrary processes. Do not run setup while the model server is using its binaries.

First start initializes the local project only when no project configuration exists. Starting an existing project preserves its goal, facts, run ID, usage ledger and verification commands. A changed model binding creates a checkpoint and archives the previous active conversation using the existing handoff behavior. Repeated starts with the same binding do not create redundant handoff checkpoints. Context-related settings are tightened for the smaller local profile; an existing stricter limit is not raised.

For an existing StateTree workspace elsewhere, point at it explicitly, preserving its `.statetree` directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1 -Repo 'D:\aws hackathon\statetree'
```

This runs the updated code while using that workspace's local state. Back up your workspace before changing its model binding. Do not copy a new empty `.statetree` over the existing one. Backups and credentials remain private.

## 3. Default profile

| Setting | Value / behavior |
|---|---|
| Model | Qwen3.5-4B, Unsloth Q4_K_M GGUF |
| GPU placement | `--gpu-layers auto --fit on`, preferred GTX 1650 CUDA device |
| Fit target | 768 MiB headroom target, **not a guaranteed memory reservation** |
| Context | 4,096 tokens shared by prompt and generated response |
| Output | 256 tokens per model call; configurable |
| Simultaneous server sequences | 1 |
| Batch / microbatch | 256 / 64 |
| KV cache | f16 |
| Flash attention | Off in this conservative initial profile |
| Thinking | Disabled via `enable_thinking=false` |
| Vision | Disabled; text-only; no projector download |
| CPU threads | 4; adjustable |
| Local HTTP timeout | 300 seconds; default legacy transport remains 55 seconds outside this launcher |

Partial CPU/RAM offload is intentional. The amount that fits depends on **currently free VRAM**, Windows display usage and model overhead. A 2.74 GB GGUF is not a promise that the entire inference workload fits inside a 4 GB GPU. The launcher requires some GPU layer offload, not all layers. No speed, quality, complete fit or tokens-per-second benchmark has been established on your machine.

StateTree conservatively estimates request size using serialized bytes rather than a Qwen tokenizer. It reserves output space and prepares bounded facts/notes, but long questions, tool replies or accumulated archive references may still exceed its budget. Try a short question with **new task** selected, or compact old context in the context lab. Increasing server context also increases resource requirements; do not assume native model context is practical on this GPU.

The local workbench's only model tool remains archive reading; this change does not turn it into an autonomous shell/coding agent.

## 4. Existing GGUF or llama-server

To avoid redownloading an existing Qwen3.5-4B Q4_K_M GGUF:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Setup-GTX1650.ps1 -ModelPath 'D:\models\Qwen3.5-4B-Q4_K_M.gguf'
```

To use both existing files:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Setup-GTX1650.ps1 -ModelPath 'D:\models\Qwen3.5-4B-Q4_K_M.gguf' -ServerPath 'D:\llama-cuda\llama-server.exe'
```

Supplied files must be trusted by you. A supplied model is header-checked, not authenticated against the pinned download checksum; supply the correct architecture and quantization. A supplied binary must support Qwen3.5, the current automatic-fit flags, Jinja tool calling, and CUDA. Very old llama.cpp builds can reject these flags. Use the pinned installer to avoid mixing executables and DLLs from unrelated releases.

Use `-CacheDir 'D:\StateTree-model-cache'` on **both** setup and start to move downloads off the system drive. Keep it outside any StateTree/Git workspace so model weights cannot be included in snapshots.

## 5. Diagnostics and low-memory alternatives

In another terminal, while the model is loaded:

```powershell
nvidia-smi -l 1
```

Look for llama-server GPU memory usage and activity during a reply. Windows WDDM may not expose every per-process memory metric. The server log path is also printed at start; the positive `offloaded X/Y layers to GPU` line is the launcher's direct offload evidence.

Inspect hardware/backend enumeration without loading weights:

```powershell
.\.venv\Scripts\python.exe -m statetree.local_gpu doctor
```

Print the intended server arguments without downloads, GPU probing or project changes:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1 -DryRun
```

For an out-of-memory error, first close other GPU-heavy applications, then explicitly lower offload:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1 -GpuLayers 20
```

This is a troubleshooting starting point, **not a tested fit guarantee**. Lower positive values use more system RAM/CPU. Zero is deliberately rejected. A 2,048-token context is available with `-ContextSize 2048`, but StateTree's conservative request accounting can make it too small for longer agent/tool exchanges. Start with 4,096 unless loading itself fails.

To permit a longer response:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1 -MaxTokens 512
```

This reduces the space reserved for input. Previously tightened context limits stay tight; see `.statetree/project/project.json` before deliberately enlarging those limits. `-Timeout 600` allows a slower turn without changing cloud configuration.

| Error | Next action |
|---|---|
| Missing `strands` | Use the extracted project's `.venv`; rerun setup without `-SkipPythonInstall`. |
| `nvidia-smi` absent / CUDA device absent | Install or repair the appropriate official NVIDIA driver; restart Windows as required. |
| Missing CUDA DLL / executable fails to start | Rerun pinned setup; do not mix binary versions. Missing MSVC runtime requires the official Microsoft x64 Visual C++ Redistributable. |
| Invalid argument / unknown architecture | Use the pinned CUDA build, not an old CPU-only binary. |
| Port 8080 or 8765 busy | Stop the server you own, or use `-Port 8081 -WebPort 8766`; never terminate an unrelated process. |
| SHA-256 mismatch | Do not bypass it. Remove only the reported corrupt cached asset and retry setup. |
| GPU startup succeeds but chat fails | Inspect the server log and StateTree failed-turn evidence. Real SDK/model integration still needs local validation. |

## 6. Verification boundary

This update was developed and tested in a Linux container **without a GPU, Windows/PowerShell or the real Strands SDK**. Unit tests and real loopback-HTTP subprocess fixtures exercise the launcher, authentication, process cleanup, state preservation, downloads and path validation. Fixture log messages are test inputs, not evidence of CUDA execution. No stub Strands package or fabricated model response has been installed as a runtime fallback.

See `GPU_VALIDATION.md` for the exact executed checks and full-suite limitations. Actual binary downloads, driver compatibility, PowerShell execution, model loading, tool use and inference speed must be validated on your Windows computer. Nothing was pushed to GitHub or deployed to AWS.

## Upstream references and pinned assets

Sources inspected on September 21, 2026. The launcher uses a pinned nightly release, **not** the moving GitHub `/releases/latest` endpoint.

- Qwen model and non-thinking template instructions: https://huggingface.co/Qwen/Qwen3.5-4B
- Quantized weights metadata: https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/blob/main/Qwen3.5-4B-Q4_K_M.gguf
- Pinned quantization revision: `e87f176479d0855a907a41277aca2f8ee7a09523`
- Binary release/checksums: https://github.com/ggml-org/llama.cpp/releases/expanded_assets/b11064
- Server flags: https://github.com/ggml-org/llama.cpp/blob/b11064/tools/server/README.md
- Server authentication implementation: https://github.com/ggml-org/llama.cpp/blob/b11064/tools/server/server-http.cpp
- NVIDIA driver downloads: https://www.nvidia.com/Download/index.aspx
- Microsoft runtime guidance: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist

Full URLs and SHA-256 digests are also recorded in `statetree/gpu_download.py`. Third-party assets retain their upstream licensing; consult the original release/model repositories. The delivered source ZIP includes no model weights, NVIDIA driver, CUDA binaries or third-party font files.
