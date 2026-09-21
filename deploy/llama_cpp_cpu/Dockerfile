# Build context: deploy/llama_cpp_cpu; always build with --platform linux/arm64.
# This is a StateTree image, not an AWS Deep Learning Container. The published
# AWS llama-cpp-arm64 DLC targets Graviton3 and cannot be assumed safe on m6g.
# Run the compiler on the builder's native CPU, producing ARM64 output.
# This avoids running every compiler process through QEMU on Intel/AMD PCs.
# Match the compiler target's Debian libraries to the final runtime below.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS builder
ARG TARGETARCH
ARG BUILD_PARALLEL=2
# Upstream tag b10433, verified against the GitHub tag object.
ENV LLAMA_CPP_COMMIT=9b05354ec6fb58b4e665e9a39ebc40285c015638
RUN test "$TARGETARCH" = arm64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates cmake make git gcc-aarch64-linux-gnu g++-aarch64-linux-gnu libc6-dev-arm64-cross \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch b10433 https://github.com/ggml-org/llama.cpp.git /src/llama.cpp \
    && test "$(git -C /src/llama.cpp rev-parse HEAD)" = "$LLAMA_CPP_COMMIT"
# Upstream also builds a small asset-embedding tool that must run on the host.
# Keep this separate so an existing cross-toolchain/source cache is reusable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
# Do not use -mcpu=native: an ARM builder may be newer than Graviton2.
# GGML_CPU_ARM_ARCH emits -march= and must not be given a CPU name.
# The explicit empty value below prevents another ARM baseline overriding N1.
RUN cmake -S /src/llama.cpp -B /src/llama.cpp/build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
    -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc \
    -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++ \
    -DHOST_CXX_COMPILER=/usr/bin/g++ \
    -DCMAKE_C_FLAGS=-mcpu=neoverse-n1 \
    -DCMAKE_CXX_FLAGS=-mcpu=neoverse-n1 \
    -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH= \
    -DGGML_CPU_ALL_VARIANTS=OFF -DGGML_CPU_KLEIDIAI=OFF \
    -DGGML_BACKEND_DL=OFF -DGGML_OPENMP=ON \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_APP=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_OPENSSL=OFF \
    && cmake --build /src/llama.cpp/build --parallel "$BUILD_PARALLEL" --target llama-server \
    && aarch64-linux-gnu-readelf -h /src/llama.cpp/build/bin/llama-server | grep 'Machine:.*AArch64'

FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS model
RUN dnf install -y --setopt=install_weak_deps=False curl-minimal ca-certificates \
    && dnf clean all && mkdir -p /model /licenses
# Build-time download only. Pin both the HF revision and the actual LFS SHA256.
RUN curl --fail --location --retry 3 --output /model/Qwen3.5-4B-Q4_K_M.gguf \
    https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/720bb031aae5488eae5d6a78768e6d826662b2ae/Qwen3.5-4B-Q4_K_M.gguf \
    && printf '%s  %s\n' 00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4 /model/Qwen3.5-4B-Q4_K_M.gguf | sha256sum --check - \
    && curl --fail --location --retry 3 --output /licenses/Qwen3.5-4B-MODEL-CARD.md \
    https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/raw/720bb031aae5488eae5d6a78768e6d826662b2ae/README.md \
    && curl --fail --location --retry 3 --output /licenses/Qwen3.5-4B-LICENSE.txt \
    https://www.apache.org/licenses/LICENSE-2.0.txt

FROM debian:bookworm-slim
RUN test "$(uname -m)" = aarch64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libstdc++6 nginx python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/llama.cpp/build/bin/ /opt/llama.cpp/bin/
COPY --from=builder /src/llama.cpp/LICENSE /usr/share/licenses/statetree/llama.cpp-LICENSE
# /opt/ml/model may be mounted by SageMaker; baked weights live elsewhere.
COPY --from=model /model/ /opt/statetree/model/
COPY --from=model /licenses/ /usr/share/licenses/statetree/
COPY NOTICE /usr/share/licenses/statetree/NOTICE
COPY nginx.conf /etc/nginx/statetree.conf
COPY serve.py /opt/statetree/serve.py
ENV PATH=/opt/llama.cpp/bin:$PATH \
    LD_LIBRARY_PATH=/opt/llama.cpp/bin \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# These smoke checks require no model load and fail an incorrect image build.
RUN llama-server --version && nginx -t -c /etc/nginx/statetree.conf
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "/opt/statetree/serve.py"]
CMD ["serve"]
