FROM debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl xz-utils bzip2 file pkg-config perl python3 \
    python3-pyelftools autoconf automake libtool bison re2c cmake ninja-build patch unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENV BUILD_JOBS=4
CMD ["bash", "/work/scripts/build-linux.sh"]
