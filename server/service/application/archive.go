package application

import (
	"archive/tar"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"strings"
)

type updateArchiveInfo struct {
	root          string
	expandedBytes uint64
	entries       int
}

func inspectUpdateArchive(archivePath, expectedRoot string) (updateArchiveInfo, error) {
	file, err := os.Open(archivePath)
	if err != nil {
		return updateArchiveInfo{}, err
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		return updateArchiveInfo{}, fmt.Errorf("open gzip archive: %w", err)
	}
	defer reader.Close()

	info := updateArchiveInfo{root: expectedRoot}
	seen := make(map[string]struct{})
	hasRoot, hasVersion := false, false
	tarReader := tar.NewReader(reader)
	for {
		header, err := tarReader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return updateArchiveInfo{}, fmt.Errorf("read tar archive: %w", err)
		}
		name, err := validateArchiveHeader(header, expectedRoot)
		if err != nil {
			return updateArchiveInfo{}, err
		}
		if _, ok := seen[name]; ok {
			return updateArchiveInfo{}, fmt.Errorf("duplicate archive entry %q", name)
		}
		seen[name] = struct{}{}
		info.entries++
		if info.entries > maxArchiveEntries {
			return updateArchiveInfo{}, fmt.Errorf("update archive has more than %d entries", maxArchiveEntries)
		}
		if name == expectedRoot && header.Typeflag == tar.TypeDir {
			hasRoot = true
		}
		if name == expectedRoot+"/version" && isRegularType(header.Typeflag) {
			hasVersion = true
		}
		if isRegularType(header.Typeflag) {
			if header.Size < 0 {
				return updateArchiveInfo{}, fmt.Errorf("negative archive file size")
			}
			size := uint64(header.Size)
			if size > maxExpandedSize-info.expandedBytes {
				return updateArchiveInfo{}, fmt.Errorf("update archive expanded size exceeds %d bytes", maxExpandedSize)
			}
			if info.expandedBytes > math.MaxUint64-size {
				return updateArchiveInfo{}, fmt.Errorf("update archive expanded size overflow")
			}
			info.expandedBytes += size
		}
	}
	if !hasRoot {
		return updateArchiveInfo{}, fmt.Errorf("invalid update package layout: missing top-level directory")
	}
	if !hasVersion {
		return updateArchiveInfo{}, fmt.Errorf("invalid update package layout: missing version file")
	}
	return info, nil
}

func extractUpdateArchive(archivePath, destDir, expectedRoot string) (string, error) {
	file, err := os.Open(archivePath)
	if err != nil {
		return "", err
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		return "", fmt.Errorf("open gzip archive: %w", err)
	}
	defer reader.Close()

	seen := make(map[string]struct{})
	entries := 0
	expanded := uint64(0)
	tarReader := tar.NewReader(reader)
	for {
		header, err := tarReader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return "", fmt.Errorf("read tar archive: %w", err)
		}
		name, err := validateArchiveHeader(header, expectedRoot)
		if err != nil {
			return "", err
		}
		if _, ok := seen[name]; ok {
			return "", fmt.Errorf("duplicate archive entry %q", name)
		}
		seen[name] = struct{}{}
		entries++
		if entries > maxArchiveEntries {
			return "", fmt.Errorf("update archive has more than %d entries", maxArchiveEntries)
		}

		filename := filepath.Join(destDir, filepath.FromSlash(name))
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(filename, 0o755); err != nil {
				return "", fmt.Errorf("create archive directory: %w", err)
			}
		case tar.TypeReg, tar.TypeRegA:
			if header.Size < 0 {
				return "", fmt.Errorf("negative archive file size")
			}
			size := uint64(header.Size)
			if size > maxExpandedSize-expanded {
				return "", fmt.Errorf("update archive expanded size exceeds %d bytes", maxExpandedSize)
			}
			expanded += size
			if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
				return "", fmt.Errorf("create archive parent directory: %w", err)
			}
			out, err := os.OpenFile(filename, os.O_WRONLY|os.O_CREATE|os.O_EXCL, os.FileMode(header.Mode)&0o777)
			if err != nil {
				return "", fmt.Errorf("create archive file: %w", err)
			}
			written, copyErr := io.CopyN(out, tarReader, header.Size)
			closeErr := out.Close()
			if copyErr != nil {
				return "", fmt.Errorf("write archive file: %w", copyErr)
			}
			if written != header.Size {
				return "", fmt.Errorf("truncated archive file %q", name)
			}
			if closeErr != nil {
				return "", fmt.Errorf("close archive file: %w", closeErr)
			}
		}
	}
	return filepath.Join(destDir, expectedRoot), nil
}

func validateArchiveHeader(header *tar.Header, expectedRoot string) (string, error) {
	if header.Name == "" || path.IsAbs(header.Name) {
		return "", fmt.Errorf("invalid archive path %q", header.Name)
	}
	name := path.Clean(header.Name)
	if name == "." || name == ".." || strings.HasPrefix(name, "../") {
		return "", fmt.Errorf("invalid archive path %q", header.Name)
	}
	if name != expectedRoot && !strings.HasPrefix(name, expectedRoot+"/") {
		return "", fmt.Errorf("invalid update package layout: entry %q is outside %s", header.Name, expectedRoot)
	}
	if !isRegularType(header.Typeflag) && header.Typeflag != tar.TypeDir {
		return "", fmt.Errorf("unsupported archive entry type for %q", header.Name)
	}
	return name, nil
}

func isRegularType(typeFlag byte) bool {
	return typeFlag == tar.TypeReg || typeFlag == tar.TypeRegA
}

func validateExtractedPackage(rootDir, expectedVersion string) error {
	version, err := os.ReadFile(filepath.Join(rootDir, "version"))
	if err != nil {
		return fmt.Errorf("invalid update package layout: read version: %w", err)
	}
	if strings.TrimSpace(string(version)) != expectedVersion {
		return fmt.Errorf("invalid update package layout: version mismatch")
	}
	for _, required := range requiredPackageFiles {
		info, err := os.Stat(filepath.Join(rootDir, filepath.FromSlash(required)))
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("invalid update package layout: missing required file %s", required)
		}
	}
	return checkLibraryList(rootDir)
}

// checkLibraryList checks server/dl_lib against the package's own list (server/dl_lib.list,
// written by package.sh): exactly the listed libraries, each a regular file. The list belongs to
// the new version, so a release may drop or add libraries; the release build checks the list
// against the tracked server/dl_lib.
func checkLibraryList(rootDir string) error {
	data, err := os.ReadFile(filepath.Join(rootDir, "server", "dl_lib.list"))
	if err != nil {
		return fmt.Errorf("invalid update package layout: read server/dl_lib.list: %w", err)
	}
	listed := map[string]struct{}{}
	for _, line := range strings.Split(string(data), "\n") {
		name := strings.TrimSpace(line)
		if name == "" {
			continue
		}
		if strings.ContainsAny(name, "/\\") || name == "." || name == ".." {
			return fmt.Errorf("invalid update package layout: bad library name %q in server/dl_lib.list", name)
		}
		listed[name] = struct{}{}
	}
	if _, ok := listed["libkvm.so"]; !ok {
		return fmt.Errorf("invalid update package layout: server/dl_lib.list does not name libkvm.so")
	}
	entries, err := os.ReadDir(filepath.Join(rootDir, "server", "dl_lib"))
	if err != nil {
		return fmt.Errorf("invalid update package layout: read server/dl_lib: %w", err)
	}
	present := map[string]struct{}{}
	for _, entry := range entries {
		if !entry.Type().IsRegular() {
			return fmt.Errorf("invalid update package layout: server/dl_lib/%s is not a regular file", entry.Name())
		}
		present[entry.Name()] = struct{}{}
	}
	for name := range listed {
		if _, ok := present[name]; !ok {
			return fmt.Errorf("invalid update package layout: missing library server/dl_lib/%s", name)
		}
	}
	for name := range present {
		if _, ok := listed[name]; !ok {
			return fmt.Errorf("invalid update package layout: server/dl_lib/%s is not in server/dl_lib.list", name)
		}
	}
	return nil
}

// requiredPackageFiles must all be in an update package. akkijp fork: besides the upstream
// four, the migration marker and every file the migration copies unconditionally
// (support/sg2002/kvm_system/main/lib/system_init/system_init.cpp new_app_init), so a package
// that would fail its first start is refused before the running version is stopped.
var requiredPackageFiles = []string{
	"version",
	"server/NanoKVM-Server",
	"kvm_system/kvm_system",
	"system/init.d/S95nanokvm",
	"system/update-nanokvm.py",
	"kvm_new_app",
	"system/init.d/S00kmod",
	"system/init.d/S01fs",
	"system/init.d/S03usbdev",
	"system/init.d/S15kvmhwd",
	"system/init.d/S30eth",
	"system/init.d/S30wifi",
	"system/init.d/S50sshd",
	"system/init.d/S98tailscaled",
	"system/ko/soph_mipi_rx.ko",
	"server/web/index.html",
	"server/dl_lib.list",
	"server/dl_lib/libkvm.so",
}

// requiredRuntimeLibraries are the server's libraries (libkvm.so and everything it loads). Every
// package ships all of them (scripts/package.sh backfills from server/dl_lib), so a package missing
// one would leave the server unable to start. Keep in sync with server/dl_lib/ (a test checks it).
