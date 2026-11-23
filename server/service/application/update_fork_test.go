package application

import (
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// akkijp fork: walk the online update path the way GitHub serves it. latest.json comes from the
// "latest" alias, the package from the versioned tag, and both redirect to another host.
func TestOnlineUpdateFollowsReleaseRedirects(t *testing.T) {
	pkg := []byte("fake package bytes")
	sum := sha512.Sum512(pkg)
	manifest, _ := json.Marshal(Latest{
		ManifestVersion: 2, Version: "2.5.4.1", Name: "nanokvm_2.5.4.1.tar.gz",
		Sha512: base64.StdEncoding.EncodeToString(sum[:]), LegacySize: uint64(len(pkg)),
		SizeBytes: uint64(len(pkg)), UnpackedSizeBytes: 100,
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
	got, _ := os.ReadFile(target)
	if string(got) != string(pkg) {
		t.Fatal("downloaded package differs")
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
