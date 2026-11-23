package application

import (
	"crypto/sha256"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

// akkijp fork: X.Y.Z.N versions must be accepted end to end.
func TestValidateLatestAcceptsFourPartVersion(t *testing.T) {
	latest := validLatest()
	latest.Version, latest.Name = "2.5.4.1", "nanokvm_2.5.4.1.tar.gz"
	if err := validateLatest(&latest); err != nil {
		t.Fatalf("four-part version rejected: %v", err)
	}
}

func TestValidateLatestRejectsMalformedVersions(t *testing.T) {
	// Name and version stay consistent so that each case fails on the format check itself,
	// not on the later name/version comparison.
	for _, v := range []string{"2.5.4.1.2", "2.5", "2.5.4.", "2.5.4.x", "../2.5.4"} {
		latest := validLatest()
		latest.Version, latest.Name = v, "nanokvm_"+v+".tar.gz"
		if err := validateLatest(&latest); err == nil || err.Error() != "invalid latest version" {
			t.Errorf("version %q: got %v, want invalid latest version", v, err)
		}
	}
	for _, v := range []string{"2.5.4.1-rc1", "2.5.4+build"} {
		latest := validLatest()
		latest.Version, latest.Name = v, "nanokvm_"+v+".tar.gz"
		if err := validateLatest(&latest); err == nil || err.Error() != "invalid update package name" {
			t.Errorf("name for %q: got %v, want invalid update package name", v, err)
		}
	}
	for _, n := range []string{"nanokvm_2.5.4.1.zip", "other_2.5.4.1.tar.gz"} {
		latest := validLatest()
		latest.Version, latest.Name = "2.5.4.1", n
		if err := validateLatest(&latest); err == nil || err.Error() != "invalid update package name" {
			t.Errorf("name %q: got %v, want invalid update package name", n, err)
		}
	}
}

func TestUpdateURLsPointAtFork(t *testing.T) {
	for _, u := range []string{StableURL, PreviewURL} {
		if !strings.HasPrefix(u, "https://github.com/akkijp/NanoKVM/") || strings.Contains(u, "sipeed.com") {
			t.Errorf("update URL %q must point at the akkijp fork", u)
		}
	}
}

func TestValidateLatestRejectsNameForOtherVersion(t *testing.T) {
	latest := validLatest()
	latest.Version, latest.Name = "2.5.4.1", "nanokvm_2.5.4.2.tar.gz"
	if err := validateLatest(&latest); err == nil {
		t.Fatal("package name for another version must be rejected")
	}
}

func TestUpdateURLsAreExact(t *testing.T) {
	if StableURL != "https://github.com/akkijp/NanoKVM/releases/latest/download" {
		t.Errorf("StableURL = %q", StableURL)
	}
	if ReleaseDownloadURL != "https://github.com/akkijp/NanoKVM/releases/download" {
		t.Errorf("ReleaseDownloadURL = %q", ReleaseDownloadURL)
	}
	if updateBaseURL != StableURL || releaseDownloadBaseURL != ReleaseDownloadURL {
		t.Error("update sources must default to the fork's releases")
	}
}

func TestOfflineUpdateRequiresSHA256(t *testing.T) {
	file := filepath.Join(t.TempDir(), "nanokvm_2.5.4.1.tar.gz")
	if err := os.WriteFile(file, []byte("package"), 0o600); err != nil {
		t.Fatal(err)
	}
	good := sha256.Sum256([]byte("package"))
	bad := sha256.Sum256([]byte("other"))

	if _, err := parseSHA256Checksum(""); err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("empty SHA-256: got %v, want required", err)
	}
	if err := verifySHA256Checksum(file, nil); err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("missing digest: got %v, want required", err)
	}
	if err := verifySHA256Checksum(file, bad[:]); err == nil || !strings.Contains(err.Error(), "mismatch") {
		t.Fatalf("wrong digest: got %v, want mismatch", err)
	}
	if err := verifySHA256Checksum(file, good[:]); err != nil {
		t.Fatalf("matching digest rejected: %v", err)
	}
}

// The offline endpoint refuses a package without a digest before it reads the upload.
func TestOfflineUpdateWithoutDigestIsRefusedFirst(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/api/application/update/offline", strings.NewReader("not multipart"))
	err := offlineUpdate(c)
	if err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("got %v, want sha256 required", err)
	}
}

func TestUpdateLockIsSharedThroughTheLockFile(t *testing.T) {
	old := updateLockPath
	updateLockPath = filepath.Join(t.TempDir(), "update.lock")
	defer func() { updateLockPath = old }()

	other, err := os.OpenFile(updateLockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(other.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	if acquireUpdateLock() {
		releaseUpdateLock()
		t.Fatal("update lock acquired while the maintenance updater holds it")
	}
	_ = other.Close()

	if !acquireUpdateLock() {
		t.Fatal("update lock not acquired once free")
	}
	releaseUpdateLock()
}

func TestPreviewCannotBeEnabledAndReportsEffectiveValue(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &Service{}

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/application/preview", strings.NewReader(`{"enable":true}`))
	c.Request.Header.Set("Content-Type", "application/json")
	s.SetPreview(c)
	if !strings.Contains(w.Body.String(), "disabled in this build") {
		t.Fatalf("enabling preview must be refused, got %s", w.Body.String())
	}

	w = httptest.NewRecorder()
	c, _ = gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/application/preview", nil)
	s.GetPreview(c)
	if !strings.Contains(w.Body.String(), `"enabled":false`) {
		t.Fatalf("preview must report false, got %s", w.Body.String())
	}
}

func TestFailedRestartReleasesUpdateLock(t *testing.T) {
	oldPath, oldCmd := updateLockPath, restartCommand
	updateLockPath = filepath.Join(t.TempDir(), "update.lock")
	restartCommand = []string{"false"}
	defer func() { updateLockPath, restartCommand = oldPath, oldCmd }()

	if !acquireUpdateLock() {
		t.Fatal("could not take the update lock")
	}
	restartServices()

	other, err := os.OpenFile(updateLockPath, os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if err := syscall.Flock(int(other.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal("the maintenance updater still cannot take the lock after a failed restart")
	}
}

func TestFailedRestartBlocksNewInstallsAndRetriesOnlyTheRestart(t *testing.T) {
	gin.SetMode(gin.TestMode)
	oldPath, oldCmd, oldDelay, oldPending := updateLockPath, restartCommand, restartDelay, restartPendingPath
	dir := t.TempDir()
	updateLockPath = filepath.Join(dir, "update.lock")
	restartPendingPath = filepath.Join(dir, "restart-pending")
	restartCommand = []string{"false"}
	restartDelay = 0
	t.Cleanup(func() {
		releaseUpdateLock()
		updateLockPath, restartCommand, restartDelay, restartPendingPath = oldPath, oldCmd, oldDelay, oldPending
	})

	if !acquireUpdateLock() {
		t.Fatal("could not take the update lock")
	}
	restartServices() // fails: the old process keeps running
	if !isRestartPending() {
		t.Fatal("a failed restart must be remembered")
	}
	if _, err := os.Stat(restartPendingPath); err != nil {
		t.Fatal("the pending restart must be visible to the maintenance updater")
	}

	// The version query tells the UI.
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/application/version", nil)
	(&Service{}).GetVersion(c)
	if !strings.Contains(w.Body.String(), `"restartPending":true`) {
		t.Fatalf("version during a pending restart: %s", w.Body.String())
	}

	// A new update request retries the restart and installs nothing.
	marker := filepath.Join(dir, "restarted")
	restartCommand = []string{"sh", "-c", "touch " + marker + "; exit 1"} // still failing, so the lock is released
	w = httptest.NewRecorder()
	c, _ = gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/application/update", nil)
	(&Service{}).Update(c)
	if !strings.Contains(w.Body.String(), "not running yet") {
		t.Fatalf("update during a pending restart: %s", w.Body.String())
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(marker); err == nil && acquireUpdateLock() {
			break // the retry ran and, having failed, released the lock
		}
		if time.Now().After(deadline) {
			t.Fatal("the restart was not retried")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
