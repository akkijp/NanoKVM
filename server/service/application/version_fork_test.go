package application

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"mime/multipart"
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
	oldPath, oldCmd, oldPending, oldDelay := updateLockPath, restartCommand, restartPendingPath, restartDelay
	dir := t.TempDir()
	updateLockPath = filepath.Join(dir, "update.lock")
	restartPendingPath = filepath.Join(dir, "restart-pending")
	restartCommand = []string{"false"}
	restartDelay = 0
	t.Cleanup(func() {
		updateLockPath, restartCommand, restartPendingPath, restartDelay = oldPath, oldCmd, oldPending, oldDelay
	})

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

func usePendingTestPaths(t *testing.T) string {
	t.Helper()
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
	return dir
}

func call(handler func(*Service, *gin.Context), method, path string) string {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(method, path, nil)
	handler(&Service{}, c)
	return w.Body.String()
}

func TestPendingUpdateBlocksInstallsAndOnlyTheRestartIsRetried(t *testing.T) {
	dir := usePendingTestPaths(t)
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	processStart = time.Now().Add(-time.Hour) // this server predates the record: the restart failed
	t.Cleanup(func() { processStart = time.Now() })

	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPending":true`) || !strings.Contains(body, `"restartPhase":"installed"`) {
		t.Fatalf("version: %s", body)
	}
	for _, handler := range []func(*Service, *gin.Context){(*Service).Update, (*Service).OfflineUpdate} {
		if body := call(handler, http.MethodPost, "/api/application/update"); !strings.Contains(body, "nothing was installed") {
			t.Fatalf("update while pending: %s", body)
		}
		waitFor(t, lockIsFree)
	}

	marker := filepath.Join(dir, "restarted")
	restartCommand = []string{"sh", "-c", "touch " + marker + "; exit 1"} // fails, so the lock is released
	if body := call((*Service).RetryRestart, http.MethodPost, "/api/application/update/restart"); !strings.Contains(body, `"restarting":true`) {
		t.Fatalf("retry: %s", body)
	}
	waitFor(t, func() bool {
		_, err := os.Stat(marker)
		return err == nil && lockIsFree()
	})
	if !isRestartPending() {
		t.Fatal("a failed restart keeps the record")
	}
}

func TestRetryRestartDoesNothingHalfWayOrWithoutARecord(t *testing.T) {
	dir := usePendingTestPaths(t)
	marker := filepath.Join(dir, "restarted")
	restartCommand = []string{"touch", marker}

	if body := call((*Service).RetryRestart, http.MethodPost, "/api/application/update/restart"); !strings.Contains(body, `"restarting":false`) {
		t.Fatalf("retry without a record: %s", body)
	}
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseSwitching}); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).RetryRestart, http.MethodPost, "/api/application/update/restart"); !strings.Contains(body, "FORK.md") {
		t.Fatalf("retry half-way: %s", body)
	}
	if err := os.WriteFile(restartPendingPath, []byte("garbage"), 0o644); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).RetryRestart, http.MethodPost, "/api/application/update/restart"); !strings.Contains(body, "FORK.md") {
		t.Fatalf("retry with an unreadable record: %s", body)
	}
	time.Sleep(50 * time.Millisecond)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the app was restarted although /kvmapp may be incomplete")
	}
}

// lockIsFree takes the update lock if it can and gives it back; it never releases a lock it did
// not take, so a handler that forgets to release it is caught.
func lockIsFree() bool {
	if !acquireUpdateLock() {
		return false
	}
	releaseUpdateLock()
	return true
}

func waitFor(t *testing.T, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestInstallRecordsThePendingRestartBeforeSwitching(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup := restartPendingPath, installAppDir, installBackupDir
	restartPendingPath = filepath.Join(dir, "missing-dir", "restart-pending") // cannot be written
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	oldTrashForTest := installTrashDir
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() { installTrashDir = oldTrashForTest })
	t.Cleanup(func() { restartPendingPath, installAppDir, installBackupDir = oldPending, oldApp, oldBackup })
	if err := os.MkdirAll(filepath.Join(dir, "new"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "new", "version"), []byte("2.5.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err == nil || !strings.Contains(err.Error(), "record the pending restart") {
		t.Fatalf("install without a record must not start: %v", err)
	}
}

func TestRestartedServerReportsConfirmingAndRetryDoesNothing(t *testing.T) {
	dir := usePendingTestPaths(t)
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	processStart = time.Now().Add(time.Hour) // started after the record: the restart happened
	t.Cleanup(func() { processStart = time.Now() })
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"confirming"`) {
		t.Fatalf("version while confirming: %s", body)
	}
	marker := filepath.Join(dir, "restarted")
	restartCommand = []string{"touch", marker}
	if body := call((*Service).RetryRestart, http.MethodPost, "/api/application/update/restart"); !strings.Contains(body, `"restarting":false`) {
		t.Fatalf("retry while confirming: %s", body)
	}
	time.Sleep(50 * time.Millisecond)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a running, restarted KVM was restarted again")
	}
}

func TestVersionReportsMicrophoneStatus(t *testing.T) {
	usePendingTestPaths(t)
	old := microphoneStatus
	microphoneStatus = filepath.Join(t.TempDir(), "mic")
	t.Cleanup(func() { microphoneStatus = old })
	if err := os.WriteFile(microphoneStatus, []byte("FAILED: capture still present on card 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"microphone":"FAILED: capture still present on card 0"`) {
		t.Fatalf("version: %s", body)
	}
}

func TestRemoveIfOnlyDirs(t *testing.T) {
	dir := t.TempDir()
	empty := filepath.Join(dir, "old")
	if err := os.MkdirAll(filepath.Join(empty, "server", "dl_lib"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !removeIfOnlyDirs(empty) {
		t.Fatal("a tree of empty directories must be removed")
	}
	kept := filepath.Join(dir, "kept")
	if err := os.MkdirAll(filepath.Join(kept, "kvm"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(kept, "kvm", "fps"), []byte("30"), 0o644); err != nil {
		t.Fatal(err)
	}
	if removeIfOnlyDirs(kept) {
		t.Fatal("a tree with files must be kept")
	}
	if _, err := os.Stat(filepath.Join(kept, "kvm", "fps")); err != nil {
		t.Fatal("the remaining file was removed")
	}
}

func TestConfirmingEndsAfterTheDeadline(t *testing.T) {
	usePendingTestPaths(t)
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(restartPendingPath)
	if err != nil {
		t.Fatal(err)
	}
	// Started after the record, but long ago: kvm_system did not stay up, so retrying is allowed.
	processStart = info.ModTime().Add(time.Millisecond)
	time.Sleep(5 * time.Millisecond)
	oldDeadline := confirmDeadline
	confirmDeadline = time.Nanosecond
	t.Cleanup(func() { processStart, confirmDeadline = time.Now(), oldDeadline })
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"installed"`) {
		t.Fatalf("version after the deadline: %s", body)
	}
}

func TestRestoreExactlyRemovesFilesTheNewVersionAdded(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"version", "server/NanoKVM-Server"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, f)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, f), []byte("old"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := listFiles(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "kvm_new_app"), nil, 0o644); err != nil { // added by the new version
		t.Fatal(err)
	}
	if !restoreExactly(dir, want) {
		t.Fatal("restore with an extra file must be fixed up")
	}
	if _, err := os.Stat(filepath.Join(dir, "kvm_new_app")); err == nil {
		t.Fatal("kvm_new_app of the new version was left behind")
	}
	if err := os.Remove(filepath.Join(dir, "server/NanoKVM-Server")); err != nil {
		t.Fatal(err)
	}
	if restoreExactly(dir, want) {
		t.Fatal("a restore missing a file is not complete")
	}
}

func TestVersionReportsMicrophoneWhenLatestCannotBeFetched(t *testing.T) {
	usePendingTestPaths(t)
	oldMic, oldBase := microphoneStatus, updateBaseURL
	microphoneStatus = filepath.Join(t.TempDir(), "mic")
	updateBaseURL = "http://127.0.0.1:1/unreachable"
	t.Cleanup(func() { microphoneStatus, updateBaseURL = oldMic, oldBase })
	if err := os.WriteFile(microphoneStatus, []byte("FAILED: capture still present on card 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	body := call((*Service).GetVersion, http.MethodGet, "/api/application/version")
	if !strings.Contains(body, `"microphone":"FAILED`) || !strings.Contains(body, `"latestError"`) {
		t.Fatalf("version without network: %s", body)
	}
}

func TestWritePendingNeverLeavesAPartialRecord(t *testing.T) {
	usePendingTestPaths(t)
	first := pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseSwitching}
	if err := writePending(first); err != nil {
		t.Fatal(err)
	}
	// The directory becomes read-only: the next write fails and the first record stays whole.
	dir := filepath.Dir(restartPendingPath)
	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	if os.Geteuid() != 0 { // root ignores the permission
		if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err == nil {
			t.Fatal("writing into a read-only directory must fail")
		}
	}
	record, pending := readPending()
	if !pending || record != first {
		t.Fatalf("record after a failed write: %+v %v", record, pending)
	}
	entries, _ := os.ReadDir(dir)
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".update-restart-pending.") {
			t.Fatalf("temporary file left behind: %s", entry.Name())
		}
	}
}

func TestDirectorySyncFailureLeavesTheAppUntouched(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup, oldSync, oldTrash := restartPendingPath, installAppDir, installBackupDir, syncDir, installTrashDir
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	syncCalls := 0
	syncDir = func(string) error { syncCalls++; return fmt.Errorf("I/O error") }
	t.Cleanup(func() {
		restartPendingPath, installAppDir, installBackupDir, syncDir, installTrashDir = oldPending, oldApp, oldBackup, oldSync, oldTrash
	})
	for path, content := range map[string]string{
		filepath.Join(installAppDir, "version"): "2.5.4.1\n",
		filepath.Join(dir, "new", "version"):    "2.5.4.2\n",
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	err := installPreparedPackage(filepath.Join(dir, "new"))
	if err == nil || !strings.Contains(err.Error(), "I/O error") {
		t.Fatalf("install must stop when the record cannot be made durable, got %v", err)
	}
	if syncCalls == 0 {
		t.Fatal("the injected directory sync was never reached")
	}
	if readVersionFile(installAppDir) != "2.5.4.1" {
		t.Fatal("the app was touched")
	}
	if _, err := os.Stat(restartPendingPath); err == nil {
		t.Fatal("a record of a switch that never started was left behind")
	}
}

func TestNewFilesAreSyncedBeforeInstalledIsRecorded(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup, oldSync := restartPendingPath, installAppDir, installBackupDir, syncFilesystems
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	oldTrashForTest := installTrashDir
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() { installTrashDir = oldTrashForTest })
	var phaseAtSync string
	syncFilesystems = func() error {
		record, _ := readPending()
		phaseAtSync = record.Phase
		return nil
	}
	t.Cleanup(func() {
		restartPendingPath, installAppDir, installBackupDir, syncFilesystems = oldPending, oldApp, oldBackup, oldSync
	})
	for _, d := range []string{installAppDir, filepath.Join(dir, "new")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(installAppDir, "version"), []byte("2.5.4.1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "new", "version"), []byte("2.5.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err != nil {
		t.Fatal(err)
	}
	if phaseAtSync != phaseSwitching {
		t.Fatalf("the files were synced while the record said %q, want switching", phaseAtSync)
	}
	if record, _ := readPending(); record.Phase != phaseInstalled || readVersionFile(installAppDir) != "2.5.4.2" {
		t.Fatalf("after install: record %+v, version %q", record, readVersionFile(installAppDir))
	}
}

func TestFailedSyncKeepsSwitching(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup, oldSync := restartPendingPath, installAppDir, installBackupDir, syncFilesystems
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	oldTrashForTest := installTrashDir
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() { installTrashDir = oldTrashForTest })
	syncFilesystems = func() error { return fmt.Errorf("cannot fork") }
	t.Cleanup(func() {
		restartPendingPath, installAppDir, installBackupDir, syncFilesystems = oldPending, oldApp, oldBackup, oldSync
	})
	for _, d := range []string{installAppDir, filepath.Join(dir, "new")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(installAppDir, "version"), []byte("2.5.4.1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "new", "version"), []byte("2.5.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err == nil {
		t.Fatal("install must fail when the new files cannot be synced")
	}
	if record, pending := readPending(); !pending || record.Phase != phaseSwitching {
		t.Fatalf("record after a failed sync: %+v %v", record, pending)
	}
}

func TestSwitchingDuringARunningUpdateIsReportedAsUpdating(t *testing.T) {
	usePendingTestPaths(t)
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseSwitching}); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"switching"`) {
		t.Fatalf("version with a stopped update: %s", body)
	}
	if !acquireUpdateLock() {
		t.Fatal("could not take the update lock")
	}
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"updating"`) {
		t.Fatalf("version during an update: %s", body)
	}
	releaseUpdateLock()

	// The maintenance updater holds the lock file.
	other, err := os.OpenFile(updateLockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if err := syscall.Flock(int(other.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"updating"`) {
		t.Fatalf("version during a maintenance update: %s", body)
	}
}

func TestVersionPartsAreOneToNineDigits(t *testing.T) {
	longest := "999999999.999999999.999999999.999999999"
	latest := validLatest()
	latest.Version, latest.Name = longest, "nanokvm_"+longest+".tar.gz"
	if err := validateLatest(&latest); err != nil {
		t.Fatalf("longest allowed version rejected: %v", err)
	}
	if err := validateFilename(latest.Name); err != nil {
		t.Fatalf("longest allowed package name rejected: %v", err)
	}
	tooLong := "1234567890.0.0.1"
	latest.Version, latest.Name = tooLong, "nanokvm_"+tooLong+".tar.gz"
	if err := validateLatest(&latest); err == nil {
		t.Fatal("a 10-digit part must be rejected")
	}
}

func TestWriteBackErrorKeepsSwitching(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup, oldFsync := restartPendingPath, installAppDir, installBackupDir, fsyncPath
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	oldTrashForTest := installTrashDir
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() { installTrashDir = oldTrashForTest })
	t.Cleanup(func() {
		restartPendingPath, installAppDir, installBackupDir, fsyncPath = oldPending, oldApp, oldBackup, oldFsync
	})
	for _, d := range []string{installAppDir, filepath.Join(dir, "new", "server")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for path, content := range map[string]string{
		filepath.Join(installAppDir, "version"):               "2.5.4.1\n",
		filepath.Join(dir, "new", "version"):                  "2.5.4.2\n",
		filepath.Join(dir, "new", "server", "NanoKVM-Server"): "new",
	} {
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// The write-back of one new file fails (an I/O error reported by fsync).
	fsyncPath = func(path string) error {
		if strings.HasSuffix(path, "NanoKVM-Server") {
			return fmt.Errorf("input/output error")
		}
		return oldFsync(path)
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err == nil || !strings.Contains(err.Error(), "input/output error") {
		t.Fatalf("install with a write-back error: %v", err)
	}
	// Caught in the stage: nothing recorded, the app untouched.
	if _, pending := readPending(); pending {
		t.Fatal("a write-back error in the stage must not leave a record")
	}
	if readVersionFile(installAppDir) != "2.5.4.1" {
		t.Fatal("the app was touched")
	}
}

func TestPreviousBackupIsRemovedThroughTheTrash(t *testing.T) {
	dir := t.TempDir()
	oldBackup, oldTrash := installBackupDir, installTrashDir
	installBackupDir, installTrashDir = filepath.Join(dir, "old"), filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() { installBackupDir, installTrashDir = oldBackup, oldTrash })
	// A removal cut short earlier left the trash; a complete backup is in place.
	for _, d := range []string{filepath.Join(installTrashDir, "kvm"), filepath.Join(installBackupDir, "server")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := removePreviousBackup(); err != nil {
		t.Fatal(err)
	}
	for _, d := range []string{installBackupDir, installTrashDir} {
		if _, err := os.Lstat(d); err == nil {
			t.Fatalf("%s is still there", d)
		}
	}
}

func TestInstallRefusesAnUnreadablePreviousVersion(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup := restartPendingPath, installAppDir, installBackupDir
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	t.Cleanup(func() { restartPendingPath, installAppDir, installBackupDir = oldPending, oldApp, oldBackup })
	for path, content := range map[string]string{
		filepath.Join(installAppDir, "version"): "\"2.5.4.1\"\n",
		filepath.Join(dir, "new", "version"):    "2.5.4.2\n",
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err == nil || !strings.Contains(err.Error(), "not a version") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(restartPendingPath); err == nil {
		t.Fatal("nothing may be recorded")
	}
}

func TestOfflineUpdateWithWrongSHA256ChangesNothing(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dir := usePendingTestPaths(t)
	oldApp, oldBackup, oldCache, oldSentinel := installAppDir, installBackupDir, installCacheDir, sentinelPath
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	installCacheDir, sentinelPath = filepath.Join(dir, "cache"), filepath.Join(dir, "sentinel")
	t.Cleanup(func() {
		installAppDir, installBackupDir, installCacheDir, sentinelPath = oldApp, oldBackup, oldCache, oldSentinel
	})
	if err := os.MkdirAll(installAppDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(installAppDir, "version"), []byte("2.5.4.1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	pkg, err := os.ReadFile(fourPartArchive(t, "2.5.4.2", "2.5.4.2"))
	if err != nil {
		t.Fatal(err)
	}
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	part, err := form.CreateFormFile("file", "nanokvm_2.5.4.2.tar.gz")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write(pkg); err != nil {
		t.Fatal(err)
	}
	if err := form.Close(); err != nil {
		t.Fatal(err)
	}
	wrong := sha256.Sum256([]byte("another package"))
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/application/update/offline", &body)
	c.Request.Header.Set("Content-Type", form.FormDataContentType())
	c.Request.Header.Set("X-SHA256-Checksum", hex.EncodeToString(wrong[:]))
	(&Service{}).OfflineUpdate(c)
	if !strings.Contains(w.Body.String(), "sha256 checksum mismatch") {
		t.Fatalf("offline update with a wrong SHA-256: %s", w.Body.String())
	}
	if readVersionFile(installAppDir) != "2.5.4.1" {
		t.Fatal("the app was touched")
	}
	if _, err := os.Stat(restartPendingPath); err == nil {
		t.Fatal("a record was written")
	}
}

func TestInstallRefusesWhenTheInstalledVersionCannotBeRead(t *testing.T) {
	dir := t.TempDir()
	oldPending, oldApp, oldBackup, oldTrash := restartPendingPath, installAppDir, installBackupDir, installTrashDir
	restartPendingPath = filepath.Join(dir, "restart-pending")
	installAppDir, installBackupDir = filepath.Join(dir, "kvmapp"), filepath.Join(dir, "old")
	installTrashDir = filepath.Join(dir, ".kvm-update", "old.removing")
	t.Cleanup(func() {
		restartPendingPath, installAppDir, installBackupDir, installTrashDir = oldPending, oldApp, oldBackup, oldTrash
	})
	// "version" is a directory: reading it fails with an error other than "does not exist".
	for _, d := range []string{filepath.Join(installAppDir, "version"), filepath.Join(installBackupDir, "server"), filepath.Join(dir, "new")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "new", "version"), []byte("2.5.4.2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := installPreparedPackage(filepath.Join(dir, "new")); err == nil || !strings.Contains(err.Error(), "cannot read the installed version") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(filepath.Join(installBackupDir, "server")); err != nil {
		t.Fatal("the previous backup was removed")
	}
}

func TestVersionAPITrimsLikeThePackageCheck(t *testing.T) {
	dir := usePendingTestPaths(t)
	oldApp := installAppDir
	installAppDir = filepath.Join(dir, "kvmapp")
	t.Cleanup(func() { installAppDir = oldApp })
	if err := os.MkdirAll(installAppDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(installAppDir, "version"), []byte("2.5.4.1\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writePending(pendingRecord{From: "2.5.4", To: "2.5.4.1", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"current":"2.5.4.1"`) {
		t.Fatalf("a CRLF version file must read as 2.5.4.1: %s", body)
	}
}

func TestRestartUnderWayIsNotOfferedForRetry(t *testing.T) {
	usePendingTestPaths(t)
	if err := writePending(pendingRecord{From: "2.5.4.1", To: "2.5.4.2", Phase: phaseInstalled}); err != nil {
		t.Fatal(err)
	}
	processStart = time.Now().Add(-time.Hour) // this server predates the record
	t.Cleanup(func() { processStart = time.Now(); setRestartUnderWay(false) })
	setRestartUnderWay(true)
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"restarting"`) {
		t.Fatalf("version while the restart is under way: %s", body)
	}
	setRestartUnderWay(false) // the restart command failed
	if body := call((*Service).GetVersion, http.MethodGet, "/api/application/version"); !strings.Contains(body, `"restartPhase":"installed"`) {
		t.Fatalf("version after a failed restart: %s", body)
	}
}

func TestUpdateInstallsOnlyTheVersionTheUserSaw(t *testing.T) {
	gin.SetMode(gin.TestMode)
	usePendingTestPaths(t)
	manifest, _ := json.Marshal(Latest{
		ManifestVersion: 2, Version: "2.5.4.3", Name: "nanokvm_2.5.4.3.tar.gz",
		Sha512: base64.StdEncoding.EncodeToString(make([]byte, 64)), LegacySize: 1, SizeBytes: 1, UnpackedSizeBytes: 1,
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(manifest)
	}))
	defer server.Close()
	oldBase := updateBaseURL
	updateBaseURL = server.URL
	t.Cleanup(func() { updateBaseURL = oldBase })

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/application/update", strings.NewReader(`{"version":"2.5.4.2"}`))
	c.Request.Header.Set("Content-Type", "application/json")
	(&Service{}).Update(c)
	if !strings.Contains(w.Body.String(), `"code":-5`) || !strings.Contains(w.Body.String(), "2.5.4.3 is now the latest") {
		t.Fatalf("update with a stale version: %s", w.Body.String())
	}
	if !lockIsFree() {
		t.Fatal("the lock was not released")
	}
}

func TestUpdateRefusesABodyItCannotRead(t *testing.T) {
	gin.SetMode(gin.TestMode)
	usePendingTestPaths(t)
	for _, body := range []string{`{"version":"2.5.4.`, `{}`, `{"version":""}`, `{"version":"latest"}`, `garbage`,
		"   ", strings.Repeat(" ", 4096) + `{"version":"2.5.4.2"}`} {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/api/application/update", strings.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		(&Service{}).Update(c)
		if !strings.Contains(w.Body.String(), "invalid update request") {
			t.Fatalf("body %q: %s", body, w.Body.String())
		}
		if !lockIsFree() {
			t.Fatalf("body %q: the lock was not released", body)
		}
	}
}

func TestVersionReportsWhenThisServerStarted(t *testing.T) {
	usePendingTestPaths(t)
	oldBase := updateBaseURL
	updateBaseURL = "http://127.0.0.1:1/unreachable"
	t.Cleanup(func() { updateBaseURL = oldBase })
	body := call((*Service).GetVersion, http.MethodGet, "/api/application/version")
	if !strings.Contains(body, fmt.Sprintf(`"serverStarted":%d`, processStart.UnixMilli())) {
		t.Fatalf("version: %s", body)
	}
}
