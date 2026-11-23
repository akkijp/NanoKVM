package application

import (
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fourPartArchive writes a minimal package nanokvm_<name>.tar.gz whose version file says inner.
func fourPartArchive(t *testing.T, name, inner string) string {
	t.Helper()
	return writeTestArchive(t, packageEntries("nanokvm_"+name, inner, ""))
}

// The offline path derives root and version from the uploaded file name.
func TestOfflinePackageKeepsFourPartVersion(t *testing.T) {
	name := "nanokvm_2.5.4.1.tar.gz"
	if err := validateFilename(name); err != nil {
		t.Fatalf("validateFilename: %v", err)
	}
	root, version := packageRootAndVersion(name)
	if root != "nanokvm_2.5.4.1" || version != "2.5.4.1" {
		t.Fatalf("packageRootAndVersion = %q, %q", root, version)
	}
	extracted, err := extractUpdateArchive(fourPartArchive(t, "2.5.4.1", "2.5.4.1"), t.TempDir(), root)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateExtractedPackage(extracted, version); err != nil {
		t.Fatalf("four-part package rejected: %v", err)
	}

	mismatched, err := extractUpdateArchive(fourPartArchive(t, "2.5.4.1", "2.5.4"), t.TempDir(), root)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateExtractedPackage(mismatched, version); err == nil {
		t.Fatal("a package whose version file says 2.5.4 must be rejected as 2.5.4.1")
	}
}

func TestExtractedPackageNeedsEveryMigrationFile(t *testing.T) {
	root := "nanokvm_2.5.4.1"
	for _, missing := range requiredPackageFiles[1:] {
		archive := writeTestArchive(t, packageEntries(root, "2.5.4.1", missing))
		extracted, err := extractUpdateArchive(archive, t.TempDir(), root)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateExtractedPackage(extracted, "2.5.4.1"); err == nil || !strings.Contains(err.Error(), missing) {
			t.Errorf("package without %s: got %v", missing, err)
		}
	}
}

func TestShippedAppHasEveryRequiredFile(t *testing.T) {
	for _, file := range requiredPackageFiles {
		switch file {
		case "version", "server/NanoKVM-Server", "kvm_system/kvm_system", "server/dl_lib/libkvm.so", "server/web/index.html":
			continue // produced by the build
		}
		if _, err := os.Stat(filepath.Join("..", "..", "..", "kvmapp", filepath.FromSlash(file))); err != nil {
			t.Errorf("kvmapp/%s is required in packages but missing from the repository", file)
		}
	}
}

// akkijp fork: walk the online update path the way GitHub serves it. latest.json comes from the
// "latest" alias, the package from the versioned tag, and both redirect to another host.
func TestOnlineUpdateFollowsReleaseRedirects(t *testing.T) {
	archivePath := fourPartArchive(t, "2.5.4.1", "2.5.4.1")
	pkg, err := os.ReadFile(archivePath)
	if err != nil {
		t.Fatal(err)
	}
	built, err := inspectUpdateArchive(archivePath, "nanokvm_2.5.4.1")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha512.Sum512(pkg)
	manifest, _ := json.Marshal(Latest{
		ManifestVersion: 2, Version: "2.5.4.1", Name: "nanokvm_2.5.4.1.tar.gz",
		Sha512: base64.StdEncoding.EncodeToString(sum[:]), LegacySize: uint64(len(pkg)),
		SizeBytes: uint64(len(pkg)), UnpackedSizeBytes: built.expandedBytes,
	})

	assets := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest.json":
			w.Header().Set("Content-Type", "application/octet-stream")
			_, _ = w.Write(manifest)
		case "/nanokvm_2.5.4.1.tar.gz":
			w.Header().Set("Content-Type", "application/octet-stream")
			_, _ = w.Write(pkg)
		default:
			http.NotFound(w, r)
		}
	}))
	defer assets.Close()

	var requested []string
	releases := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requested = append(requested, r.URL.Path)
		switch r.URL.Path {
		case "/releases/latest/download/latest.json":
			http.Redirect(w, r, assets.URL+"/latest.json", http.StatusFound)
		case "/releases/download/2.5.4.1/nanokvm_2.5.4.1.tar.gz":
			http.Redirect(w, r, assets.URL+"/nanokvm_2.5.4.1.tar.gz", http.StatusFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer releases.Close()

	oldBase, oldDownload := updateBaseURL, releaseDownloadBaseURL
	updateBaseURL = releases.URL + "/releases/latest/download"
	releaseDownloadBaseURL = releases.URL + "/releases/download"
	defer func() { updateBaseURL, releaseDownloadBaseURL = oldBase, oldDownload }()

	latest, err := getLatest()
	if err != nil {
		t.Fatalf("getLatest: %v", err)
	}
	if want := releases.URL + "/releases/download/2.5.4.1/nanokvm_2.5.4.1.tar.gz"; latest.Url != want {
		t.Fatalf("package URL = %q, want %q", latest.Url, want)
	}

	target := filepath.Join(t.TempDir(), latest.Name)
	if _, err := download(latest, target); err != nil {
		t.Fatalf("download: %v", err)
	}
	if err := checksum(target, latest.Sha512); err != nil {
		t.Fatalf("checksum: %v", err)
	}
	root := strings.TrimSuffix(latest.Name, ".tar.gz")
	info, err := inspectUpdateArchive(target, root)
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if err := validateExpandedSize(latest, info.expandedBytes); err != nil {
		t.Fatalf("expanded size: %v", err)
	}
	wrong := *latest
	wrong.UnpackedSizeBytes = info.expandedBytes + 1
	if err := validateExpandedSize(&wrong, info.expandedBytes); err == nil {
		t.Fatal("a manifest with the wrong unpacked size must be rejected")
	}
	extracted, err := extractUpdateArchive(target, t.TempDir(), root)
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if err := validateExtractedPackage(extracted, latest.Version); err != nil {
		t.Fatalf("validate: %v", err)
	}
	if len(requested) != 2 || requested[1] != "/releases/download/2.5.4.1/nanokvm_2.5.4.1.tar.gz" {
		t.Fatalf("unexpected release requests: %v", requested)
	}
}

func TestOnlineUpdateIgnoresSavedCustomServer(t *testing.T) {
	dir := t.TempDir()
	oldPath := updateServerConfigPath
	updateServerConfigPath = filepath.Join(dir, "application-update.json")
	defer func() { updateServerConfigPath = oldPath }()
	if err := saveUpdateServerConfigToPath(updateServerConfigPath, UpdateServerConfig{Enabled: true, URL: "https://cdn.sipeed.com/nanokvm"}); err != nil {
		t.Fatal(err)
	}
	base, err := resolveUpdateBaseURL()
	if err != nil || base != StableURL {
		t.Fatalf("resolveUpdateBaseURL = %q, %v; want %q", base, err, StableURL)
	}
}
