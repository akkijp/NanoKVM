# Makefile for NanoKVM Project

# Configuration
UID := $(shell id -u)
GID := $(shell id -g)
# ?= so a prebuilt image can be used instead, e.g. a GHCR-cached builder in CI.
IMAGE_NAME ?= nanokvm-builder-local-$(UID)-$(GID)
PWD := $(shell pwd)
VERSION ?=

# Docker run common parameters. Allocating a TTY breaks in environments without
# one, so it can be overridden with DOCKER_TTY=
DOCKER_TTY ?= -it
DOCKER_RUN_BASE := docker run -e UID=$(UID) -e GID=$(GID) -v $(PWD):/home/build/NanoKVM --rm

# Build the image with the host user's identity so entrypoint does not need
# to recursively rewrite the MaixCDK home directory at container startup.
DOCKER_BUILD_ARGS := --build-arg DOCKER_UID=$(UID) --build-arg DOCKER_GID=$(GID)

# The image ID builder-image produced, per checkout (akkijp fork): the builds below run that exact
# image, so another checkout re-tagging $(IMAGE_NAME) meanwhile cannot swap the SDK under them.
BUILDER_IID := $(PWD)/build/.builder-image-id
BUILDER := "$$(cat $(BUILDER_IID))"

# Build commands
GO_BUILD_CMD := cd /home/build/NanoKVM/server && ./build.sh
SUPPORT_BUILD_CMD := . ./home/build/MaixCDK/bin/activate && cd /home/build/NanoKVM/support/sg2002 && ./build kvm_system && ./build kvm_system add_to_kvmapp
VISION_BUILD_CMD := . ./home/build/MaixCDK/bin/activate && cd /home/build/NanoKVM/support/sg2002 && ./build kvm_vision && ./build kvm_vision add_to_kvmapp
RELEASE_BUILD_CMD := /home/build/NanoKVM/scripts/build-in-container.sh

.PHONY: help check-root builder-image rebuild-image check-image shell app support vision \
        web release-build package release all clean

# Default target
all: app support

# Help target
help:
	@echo "NanoKVM Build System"
	@echo ""
	@echo "Available targets:"
	@echo "  help          - Show this help message"
	@echo "  check-image   - Check builder Docker image and show versions"
	@echo "  builder-image - Build Docker image (cached layers are reused)"
	@echo "  rebuild-image - Force rebuild Docker image"
	@echo "  shell         - Enter interactive builder environment"
	@echo "  app           - Build Go application server"
	@echo "  support       - Build kvm_system daemon"
	@echo "  vision        - Build video libraries (libkvm.so)"
	@echo "  web           - Build the frontend into web/dist"
	@echo "  all           - Build both app and support (default)"
	@echo "  release-build - Build every riscv64 release artifact in one pass"
	@echo "  package       - Assemble nanokvm_<VERSION>.tar.gz + latest.json"
	@echo "  release       - release-build + web + package (needs VERSION=x.y.z)"
	@echo "  clean         - Clean build artifacts"
	@echo ""
	@echo "Prerequisites:"
	@echo "  - Docker must be installed and running"
	@echo "  - Must not run as root user"

# Security check - prevent running as root
check-root:
	@if [ "$$(id -u)" -eq 0 ]; then \
		echo "Can't run as root"; \
		exit 1; \
	fi

# Check if builder image exists and show versions
check-image: check-root
	@echo "Checking builder image..."
	@echo "Golang version: " && \
		docker run --rm -i $(IMAGE_NAME) go version && \
		echo "" && \
		echo "Host-tools version:" && \
		docker run --rm -i $(IMAGE_NAME) riscv64-unknown-linux-musl-gcc -v && \
		echo ""

# Build the Docker image from docker/Dockerfile every time. The layer cache
# keeps this fast, and a stale image can never outlive a change to the pinned
# toolchain, Go or MaixCDK versions (akkijp fork).
builder-image: check-root
	@echo "Building Docker image..."
	@mkdir -p build && docker build --platform linux/amd64 $(DOCKER_BUILD_ARGS) --iidfile $(BUILDER_IID) -t $(IMAGE_NAME) -f docker/Dockerfile ./

# Force rebuild Docker image
rebuild-image: check-root
	@echo "Force rebuilding Docker image..."
	@mkdir -p build && docker build --platform linux/amd64 --no-cache $(DOCKER_BUILD_ARGS) --iidfile $(BUILDER_IID) -t $(IMAGE_NAME) -f docker/Dockerfile ./

# Enter interactive shell (equivalent to build.sh with no arguments)
shell: check-root builder-image
	@echo "Switching into builder..."
	@$(DOCKER_RUN_BASE) -it $(BUILDER) /bin/bash -c ". ./home/build/MaixCDK/bin/activate && cd /home/build/NanoKVM ; exec bash"

# Build Go application
app: check-root builder-image
	@echo "Building app..."
	@$(DOCKER_RUN_BASE) $(DOCKER_TTY) $(BUILDER) /bin/bash -c '$(GO_BUILD_CMD)'

# Build hardware support libraries
support: check-root builder-image
	@echo "Building support..."
	@$(DOCKER_RUN_BASE) $(DOCKER_TTY) $(BUILDER) /bin/bash -c '$(SUPPORT_BUILD_CMD)'

# Build video libraries (libkvm.so) into kvmapp/server/dl_lib
vision: check-root builder-image
	@echo "Building vision..."
	@$(DOCKER_RUN_BASE) $(DOCKER_TTY) $(BUILDER) /bin/bash -c '$(VISION_BUILD_CMD)'

# Build every riscv64 release artifact in one container pass
release-build: check-root builder-image
	@echo "Building release artifacts..."
	@$(DOCKER_RUN_BASE) $(DOCKER_TTY) $(BUILDER) /bin/bash -c '$(RELEASE_BUILD_CMD)'

# Build the frontend in the pinned node container (scripts/fork-env.sh; pnpm is pinned by
# "packageManager" in web/package.json). The builder image has no Node. The source is copied
# into the container without node_modules, so a host checkout's own dependencies are neither
# used nor replaced; only dist/ is written back.
web:
	@rm -rf web/dist.new && mkdir -p web/dist.new
	@echo "Building web..."
	@. scripts/fork-env.sh && docker run --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
		-v "$(PWD)/web:/src:ro" -v "$(PWD)/web/dist.new:/out" "$$NODE_IMAGE" bash -euc \
		'mkdir -p /tmp/web && cd /src && tar --exclude=./node_modules --exclude=./dist --exclude=./dist.new -cf - . | tar -C /tmp/web -xf - && cd /tmp/web && mkdir -p /tmp/bin && corepack enable --install-directory /tmp/bin && export PATH=/tmp/bin:$$PATH && pnpm install --frozen-lockfile && pnpm build && cp -a dist/. /out/'
	@rm -rf web/dist && mv web/dist.new web/dist

# Assemble the release package and its manifest
package:
	@if [ -z "$(VERSION)" ]; then \
		echo "VERSION is required, e.g. make package VERSION=2.4.4"; \
		exit 1; \
	fi
	@scripts/package.sh "$(VERSION)"

# Full local release: riscv64 artifacts + frontend + package.
# Invoked as sub-makes rather than prerequisites: packaging asserts on what the
# earlier steps produce, so the order matters even under make -j.
release:
	@$(MAKE) release-build
	@$(MAKE) web
	@$(MAKE) package VERSION="$(VERSION)"

# Clean build artifacts
clean:
	@echo "Cleaning build artifacts..."
	@if [ -f server/NanoKVM-Server ]; then \
		rm -f server/NanoKVM-Server; \
		echo "Removed server/NanoKVM-Server"; \
	fi
	@if [ -d support/sg2002/build ]; then \
		rm -rf support/sg2002/build; \
		echo "Removed support/sg2002/build"; \
	fi
	@if [ -d build/release ]; then \
		rm -rf build/release; \
		echo "Removed build/release"; \
	fi
	@if [ -d web/dist ]; then \
		rm -rf web/dist; \
		echo "Removed web/dist"; \
	fi
	@echo "Clean completed."
