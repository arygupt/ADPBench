# syntax=docker/dockerfile:1
#
# ADPBench agent sandbox, pinned to the same substrate the host scores with:
# yosys at the commit reported by `yosys -V` and iverilog v13_0.
#
# `adpbench agent --sandbox docker` mounts the task directory at /work
# read-write and a sanitized harness bundle at /adpbench read-only. The bundle
# has the held-out evaluation seeds redacted, so the container can run
# ./check.sh on dev seeds but cannot read the final scoring cases.
#
# Build (a few minutes; yosys is compiled from source):
#   docker build -t adpbench-agent sandbox/

FROM debian:bookworm-slim AS yosys-build
ARG YOSYS_COMMIT=143eb14f9cc55d6f8927e68523b0c9d2166ed02c
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates git curl python3 zlib1g-dev libffi-dev \
      bison flex pkg-config \
 && rm -rf /var/lib/apt/lists/*
# Bookworm ships cmake 3.25; yosys needs >= 3.28.
ARG CMAKE_VERSION=3.30.5
RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      arm64) cmake_arch=linux-aarch64 ;; \
      amd64) cmake_arch=linux-x86_64 ;; \
      *) echo "unsupported architecture $arch" >&2; exit 1 ;; \
    esac; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 2 "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-${cmake_arch}.tar.gz" -o cmake.tar.gz; \
    mkdir -p /opt/cmake; tar -xzf cmake.tar.gz -C /opt/cmake --strip-components=1
ENV PATH="/opt/cmake/bin:${PATH}"
WORKDIR /src
# Fetch the exact commit the host reports, with the submodules the CMake build
# needs (slang and abc are disabled; nothing in the pinned flow uses them).
RUN git init yosys \
 && cd yosys \
 && git remote add origin https://github.com/YosysHQ/yosys.git \
 && git fetch --depth 1 origin "${YOSYS_COMMIT}" \
 && git checkout FETCH_HEAD \
 && git submodule update --init --depth 1 \
      libs/cxxopts libs/fmt libs/tomlplusplus libs/boost_regex libs/symfpu
RUN cd yosys \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DYOSYS_WITHOUT_ABC=ON -DYOSYS_WITHOUT_SLANG=ON -DYOSYS_WITHOUT_TCL=ON \
      -DYOSYS_WITHOUT_READLINE=ON -DYOSYS_WITHOUT_EDITLINE=ON \
      -DYOSYS_ENABLE_UNIT_TESTS=OFF \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build

FROM debian:bookworm-slim AS iverilog-build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl autoconf gperf flex bison \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fL --retry 5 --retry-all-errors --retry-delay 2 "https://github.com/steveicarus/iverilog/archive/refs/tags/v13_0.tar.gz" -o iverilog.tar.gz \
 && mkdir iverilog && tar -xzf iverilog.tar.gz -C iverilog --strip-components=1 \
 && cd iverilog && sh autoconf.sh && ./configure --prefix=/usr/local \
 && make -j"$(nproc)" && make install

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-numpy python-is-python3 zlib1g libffi8 tcl8.6 \
      bsdutils \
 && rm -rf /var/lib/apt/lists/*
COPY --from=yosys-build /usr/local/ /usr/local/
COPY --from=iverilog-build /usr/local/ /usr/local/
RUN ldconfig
ENV PYTHONPATH=/adpbench
WORKDIR /work

RUN yosys -V \
 && iverilog -V \
 && python -c "import numpy"
