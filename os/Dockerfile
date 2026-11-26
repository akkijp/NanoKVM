# akkijp fork: build environment for the NanoKVM OS image (sipeed/LicheeRV-Nano-Build).
# The package list is upstream's host/ubuntu/Dockerfile; the base image is pinned by digest and
# apt comes from a dated Ubuntu snapshot, so the same commit builds with the same tools. The
# snapshot is https only and the base image has no CA certificates: the bundle comes from the
# same pinned Go image as docker/Dockerfile.
FROM ubuntu:22.04@sha256:b000e69a50ff940390233025e7a337fb8469b509e2334fa4d018ac30f3e18cdb

ENV DEBIAN_FRONTEND=noninteractive
COPY --from=golang:1.25.0@sha256:5502b0e56fca23feba76dbc5387ba59c593c02ccc2f0f7355871ea9a0852cebe \
     /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

RUN sed -i 's|http://archive.ubuntu.com/ubuntu|https://snapshot.ubuntu.com/ubuntu/20261120T000000Z|; s|http://security.ubuntu.com/ubuntu|https://snapshot.ubuntu.com/ubuntu/20261120T000000Z|' /etc/apt/sources.list \
 && apt-get -o Acquire::Check-Valid-Until=false update -y \
 && ldconfig && apt-get -o Acquire::Check-Valid-Until=false install -y \
	pkg-config build-essential ninja-build \
	automake autoconf libtool wget curl git gcc libssl-dev bc \
	slib squashfs-tools android-sdk-libsparse-utils rsync \
	jq cmake python3-distutils tclsh scons parallel ssh-client tree \
	python3-dev python3-pip device-tree-compiler ssh cpio \
	fakeroot libncurses5-dev flex bison \
	android-libext4-utils proot fakechroot \
	bzr cvs file locales mercurial python3 \
	python3-flake8 python3-nose2 python3-pexpect subversion \
	unzip ccache gawk diffstat texinfo chrpath socat \
	xz-utils debianutils \
	iputils-ping python3-git python3-jinja2 python3-subunit \
	zstd liblz4-tool libacl1 parted erofs-utils genext2fs mtools \
	dosfstools libconfuse-dev gettext autoconf-archive clang \
 && rm -rf /var/lib/apt/lists/*

RUN echo en_US.UTF-8 UTF-8 >> /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID build 2>/dev/null || true; useradd -m -u $UID -g $GID build
USER build
WORKDIR /work
