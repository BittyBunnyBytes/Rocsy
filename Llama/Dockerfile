FROM archlinux:base

# llama-swap is a small Go proxy that starts/stops llama.cpp (or anything
# OpenAI-compatible) on demand based on the "model" field in the request, so
# picking a model is an API/UI concern rather than a restart-the-container
# concern. This image does NOT bundle llama.cpp itself -- the GPU workload
# stays on the host's own ROCm build (see docker-compose.yml's volumes for
# ~/AI/llama.cpp-rdna-boost and /opt/rocm), matching this
# repo's "GPU workloads stay bare-metal" convention while still giving
# llama-swap's process-management/model-picker a normal container home.
#
# Base is Arch (not Debian) on purpose: the mounted llama-server binary was
# built on this host's CachyOS (Arch-based, rolling glibc/libstdc++), and a
# glibc/libstdc++ mismatch between an older-glibc container and a
# newer-glibc host binary is a real failure mode ("GLIBC_2.3x not found").
# Arch's rolling packages track close enough to CachyOS's to link cleanly.
ARG LLAMA_SWAP_VERSION=255

# The stock mirrorlist has only fastly + geo, and a dropped TLS connection on either fails
# the whole transaction. Try geo (Arch's own GeoDNS) first, keep fastly as the fallback, and
# retry: pacman keeps what it already downloaded in its cache.
RUN printf 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\nServer = https://fastly.mirror.pkgbuild.com/$repo/os/$arch\n' \
        > /etc/pacman.d/mirrorlist \
    && for attempt in 1 2 3 4; do \
        pacman -Syu --noconfirm --needed \
            openssl gcc-libs zlib brotli zstd fmt google-glog elfutils libdrm numactl gflags \
            vulkan-icd-loader vulkan-radeon \
            ca-certificates curl tar \
        && break; \
        [ "$attempt" = 4 ] && exit 1; echo "pacman download failed, retrying ($attempt/3)"; sleep $((attempt * 5)); \
    done \
    && curl -fsSL --retry 4 --retry-all-errors "https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz" \
        -o /tmp/llama-swap.tar.gz \
    && tar -xzf /tmp/llama-swap.tar.gz -C /usr/local/bin llama-swap \
    && chmod +x /usr/local/bin/llama-swap \
    && rm -f /tmp/llama-swap.tar.gz \
    && pacman -Scc --noconfirm \
    && useradd --system --create-home --home-dir /app llama \
    && mkdir -p /app && chown llama:llama /app

# vulkan-icd-loader + vulkan-radeon (RADV): runtime for builds with llama.cpp's Vulkan
# backend (Llama Parameters -> GPU backend: Vulkan); ROCm stays the default.
# The compose service passes /dev/kfd and /dev/dri through so optional
# additional ROCm render nodes are visible to llama.cpp.
WORKDIR /app
COPY config.yaml /app/config.yaml
COPY --chmod=755 llama-launch /usr/local/bin/llama-launch
USER llama

ENV LLAMA_HOST=127.0.0.1 \
    LLAMA_PORT=8400 \
    LD_LIBRARY_PATH=/opt/rocm/lib
# LD_LIBRARY_PATH (not ldconfig) because /opt/rocm only exists once the
# volume is mounted at container start -- ldconfig at build time would find
# nothing there. libggml-hip.so.0's own RUNPATH doesn't cover /opt/rocm/lib
# (only DT_RUNPATH of the *referencing* object is consulted, not the
# executable's), and this fresh image has no /etc/ld.so.conf.d entry for it
# the way the host's ROCm package install does, so ld.so.cache doesn't
# either -- confirmed via ldd inside the built container.

EXPOSE 8400

CMD ["sh", "-c", "bin=/usr/local/bin/llama-swap; if [ -x /opt/rocsy-llama-swap/active/llama-swap ]; then bin=/opt/rocsy-llama-swap/active/llama-swap; fi; exec \"$bin\" -config /app/config.yaml -listen ${LLAMA_HOST:-127.0.0.1}:${LLAMA_PORT:-8400}"]
