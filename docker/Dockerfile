# akkijp fork: the base image is pinned by digest and packages come from a dated Ubuntu
# snapshot, so a fresh VM and one with cached layers install the same versions.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289 AS base_apt
ARG UBUNTU_SNAPSHOT=20261120T000000Z

# RUN echo 'Acquire::http::Proxy "http://cache.lan:3142";' > /etc/apt/apt.conf.d/00cacher
RUN userdel -r ubuntu
# snapshot.ubuntu.com is HTTPS-only and the base image has no CA bundle. Take the bundle from a
# digest-pinned image, so every package, ca-certificates included, comes from the snapshot.
COPY --from=golang:1.25.0@sha256:5502b0e56fca23feba76dbc5387ba59c593c02ccc2f0f7355871ea9a0852cebe \
    /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN sed -i -E "s#^URIs: http://(archive|security).ubuntu.com/ubuntu/#URIs: https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/#" \
        /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y ca-certificates wget git cmake build-essential python3 python3-venv python3-pip autoconf automake libtool gosu patchelf

ARG DOCKER_USER=build
ARG DOCKER_UID=1000
ARG DOCKER_GID=1000
# Reuse existing accounts for numeric IDs that are already present in the
# base image. All later ownership and user switches use numeric IDs, so a
# dedicated build user/group is only needed when the ID does not exist.
RUN set -eux; \
    if ! getent group "$DOCKER_GID" >/dev/null; then \
        groupadd --gid "$DOCKER_GID" "$DOCKER_USER"; \
    fi; \
    if ! getent passwd "$DOCKER_UID" >/dev/null; then \
        useradd --uid "$DOCKER_UID" --gid "$DOCKER_GID" --no-create-home \
            --home-dir "/home/$DOCKER_USER" --shell /bin/bash "$DOCKER_USER"; \
    fi; \
    install -d -o "$DOCKER_UID" -g "$DOCKER_GID" "/home/$DOCKER_USER"
USER $DOCKER_UID:$DOCKER_GID

FROM base_apt AS host_tools

USER root
# akkijp fork: the toolchain is fetched from sophon.cn, so pin its SHA-256 (recorded 2026-11-22, see FORK.md).
ARG HOST_TOOLS_SHA256=ff9a58e8e192b20ea42e1d729c42d2219523209706cb3f0cf134582f6c70f805
RUN wget -O /tmp/host-tools.tar.gz https://sophon-file.sophon.cn/sophon-prod-s3/drive/23/03/07/16/host-tools.tar.gz \
    && echo "$HOST_TOOLS_SHA256  /tmp/host-tools.tar.gz" | sha256sum -c - \
    && tar -C /usr/local -zxf /tmp/host-tools.tar.gz  \
    && rm -f /tmp/host-tools.tar.gz \
    && export PATH=$PATH:/usr/local/host-tools/gcc/riscv64-linux-musl-x86_64/bin \
    && echo "Installed host-tools gcc" && riscv64-unknown-linux-musl-gcc -v

FROM base_apt AS golang

USER root
# akkijp fork: verify the Go archive against the checksum published on go.dev. The host
# toolchain above is x86-64 only, so the whole image is built for linux/amd64.
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) arch=amd64; sum=2852af0cb20a13139b3448992e69b868e50ed0f8a1e5940ee1de9e19a123b613 ;; \
      *) echo "unsupported build host $(uname -m): the builder needs linux/amd64"; exit 1 ;; \
    esac; \
    wget -O /tmp/go.tar.gz "https://go.dev/dl/go1.25.0.linux-${arch}.tar.gz"; \
    echo "${sum}  /tmp/go.tar.gz" | sha256sum -c - \
    && tar -C /usr/local -zxf /tmp/go.tar.gz \
    && export PATH=$PATH:/usr/local/go/bin \
    && echo "Installed golang" &&  go version

COPY server /tmp/go_cache

ENV PATH="$PATH:/usr/local/go/bin"

RUN cd /tmp/go_cache \
    && echo "Caching go dep" \
    && go mod download \
    && go mod tidy \
    && cd / \
    && rm -rf /tmp/go_cache

FROM base_apt AS sdk

# akkijp fork: pin MaixCDK instead of building from whatever main is today.
ARG MAIXCDK_COMMIT=83f8b8d456813932526601a40757bdafe452e718

ENV HOME="/home/$DOCKER_USER"

# akkijp fork: install MaixCDK's Python dependencies from hash-pinned lock files instead of
# resolving requirements.txt at build time.
COPY docker/maixcdk-build-requirements.lock docker/maixcdk-requirements.lock /tmp/

RUN cd ~ \
    && git clone https://github.com/Sipeed/MaixCDK \
    && cd MaixCDK \
    && git checkout --detach "$MAIXCDK_COMMIT" \
    && python3 -m venv . \
    && . ./bin/activate \
    && pip install --no-deps --require-hashes -r /tmp/maixcdk-build-requirements.lock \
    && pip install --no-deps --require-hashes --no-build-isolation -r /tmp/maixcdk-requirements.lock \
    && pip check

COPY --chown=$DOCKER_UID:$DOCKER_GID . /home/$DOCKER_USER/NanoKVM

RUN cd ~/MaixCDK \
    && . ./bin/activate \
    && cd ~/NanoKVM/support/sg2002 \
    && ./build kvm_system

FROM base_apt

# Install golang
COPY --from=golang /usr/local/go /usr/local/go
COPY --chown=$DOCKER_UID:$DOCKER_GID --from=golang /root/go /home/$DOCKER_USER/go
ENV PATH="$PATH:/usr/local/go/bin"

# Install host tools
COPY --from=host_tools /usr/local/host-tools /usr/local/host-tools
ENV PATH="$PATH:/usr/local/host-tools/gcc/riscv64-linux-musl-x86_64/bin"

# Install SDK
COPY --chown=$DOCKER_UID:$DOCKER_GID --from=sdk /home/$DOCKER_USER/MaixCDK /home/$DOCKER_USER/MaixCDK
COPY --chmod=+x docker/entrypoint /entrypoint

RUN echo "Verify build tools" \
    && echo "Installed host-tools gcc" && riscv64-unknown-linux-musl-gcc -v \
    && echo "Installed golang" && go version

USER root
RUN chmod 0755 /entrypoint \
 && sed "s/\$DOCKER_USER/$DOCKER_USER/g" -i /entrypoint

ENTRYPOINT ["/entrypoint"]
