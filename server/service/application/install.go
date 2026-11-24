package application

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"NanoKVM-Server/utils"
	log "github.com/sirupsen/logrus"
)

var (
	// The directories install switches; variables only so tests never touch the real ones.
	installAppDir    = AppDir
	installBackupDir = BackupDir
	installCacheDir  = CacheDir

	mutex      sync.Mutex
	isUpdating bool
	updateLock *os.File

	// akkijp fork: the same lock file as kvmapp/system/update-nanokvm.py, so the web updates
	// and the maintenance updater never move /kvmapp and /root/old at the same time.
	updateLockPath = "/run/nanokvm-update.lock"
)

func acquireUpdateLock() bool {
	mutex.Lock()
	defer mutex.Unlock()

	if isUpdating {
		return false
	}
	file, err := os.OpenFile(updateLockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		log.Errorf("failed to open update lock %s: %v", updateLockPath, err)
		return false
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		log.Warnf("another update holds %s", updateLockPath)
		return false
	}
	updateLock = file
	isUpdating = true
	return true
}

func releaseUpdateLock() {
	mutex.Lock()
	defer mutex.Unlock()
	if updateLock != nil {
		_ = updateLock.Close()
		updateLock = nil
	}
	isUpdating = false
}

func installPreparedPackage(sourceDir string) error {
	// akkijp fork: from here until the new version is seen running, /root/old is the version
	// known to work. Record that before touching anything; no record, no switch (pending.go).
	previous, err := readVersionFileStrict(installAppDir)
	if err != nil {
		return fmt.Errorf("cannot read the installed version (%w); nothing was changed", err)
	}
	record := pendingRecord{From: previous, To: readVersionFile(sourceDir), Phase: phaseSwitching}
	// Both go into the record, which S95nanokvm and kvm_system parse strictly: refuse anything
	// they would not accept, before anything changes.
	if record.From != "" && !previousVersionPattern.MatchString(record.From) { // empty: no version file
		return fmt.Errorf("the installed version %q is not a version; nothing was changed", record.From)
	}
	if !versionPattern.MatchString(record.To) {
		return fmt.Errorf("the new version %q is not a version; nothing was changed", record.To)
	}
	// The new files are made final (permissions) and durable where they are staged, before
	// anything changes: fsync reports write-back errors that sync(1) cannot, and the staged tree
	// holds no runtime files that could vanish while it is walked.
	if err := utils.ChmodRecursively(sourceDir, 0o755); err != nil {
		return fmt.Errorf("failed to chmod: %w", err)
	}
	if err := fsyncTree(sourceDir); err != nil {
		return fmt.Errorf("write the new version to disk: %w", err)
	}
	// Hold S95nanokvm's service lock from here until "installed": a restart started elsewhere
	// (e.g. a TLS change) must neither stop this server half-way nor start from a half-switched
	// /kvmapp. Taken before anything changes, so a busy lock leaves even the previous backup
	// alone. The restart this update asks for comes after the lock is released.
	releaseService, err := lockServiceOperations()
	if err != nil {
		return err
	}
	defer releaseService()
	// The previous backup is renamed away and removed with nothing recorded yet: if this is cut
	// short, /kvmapp is whole and the next boot starts it; the leftover is finished next time
	// (here or by update-nanokvm.py, which uses the same place).
	if err := removePreviousBackup(); err != nil {
		return fmt.Errorf("failed to remove backup: %w", err)
	}
	if err := writePending(record); err != nil {
		// Nothing was moved. A record that became visible before the failure would stop the next
		// boot: take it back.
		_ = clearPending()
		return err
	}
	oldFiles, err := listFiles(installAppDir)
	if err != nil {
		_ = clearPending() // nothing has been moved yet
		return fmt.Errorf("list the current app: %w", err)
	}
	if err := backupCurrentApp(); err != nil {
		return err // the old app may be partly moved: keep the record as "switching"
	}

	if err := applyUpdate(sourceDir); err != nil {
		// The backup was moved back file by file; it leaves its directories behind. Only when
		// nothing but empty directories is left was the restore complete.
		// (Also for a device without a version file: the file list is the proof, not the version.)
		if removeIfOnlyDirs(installBackupDir) && restoreExactly(installAppDir, oldFiles) {
			// The old version is in place again, file for file. Make that durable before the
			// record goes (a power cut in between must keep "switching"), then make the removal
			// durable too.
			if syncErr := syncFilesystems(); syncErr != nil {
				return fmt.Errorf("%w; the previous version was put back but could not be synced (%v), so the update stays marked unfinished", err, syncErr)
			}
			if clearErr := clearPending(); clearErr != nil {
				return fmt.Errorf("%w; the previous version was put back but the record could not be removed (%v)", err, clearErr)
			}
			if syncErr := syncDir(filepath.Dir(restartPendingPath)); syncErr != nil {
				return fmt.Errorf("%w; the previous version was put back but removing the record could not be synced (%v)", err, syncErr)
			}
		}
		return err
	}

	// The moves changed directory entries: make those durable before the record says
	// "installed" (a power cut in between leaves "switching").
	if err := syncFilesystems(); err != nil {
		// Not known to be on disk: stay "switching" (the next boot will not start from it).
		return fmt.Errorf("sync the new version to disk: %w", err)
	}

	// Counted while the record still says "switching": a page that reinstalled the running
	// version sees the count move and knows the install happened (a restart alone, e.g. after a
	// power cut during the upload, does not count). Counted before "installed", a power cut in
	// between leaves "switching", which the page never takes as finished; counted after, it
	// could leave a working reinstall that the page can never confirm.
	if err := countInstall(); err != nil {
		log.Warnf("count the install: %v", err)
	}
	// Complete: only the restart is missing, which may now be retried.
	record.Phase = phaseInstalled
	if err := writePending(record); err != nil {
		return err
	}
	return nil
}

// installCountPath counts the updates installed on this device (the web updater and
// update-nanokvm.py both add one each time "installed" is recorded).
var installCountPath = "/etc/kvm/update-count"

func readInstallCount() int64 {
	data, err := os.ReadFile(installCountPath)
	if err != nil {
		return 0
	}
	n, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

func countInstall() error {
	dir := filepath.Dir(installCountPath)
	tmp, err := os.CreateTemp(dir, ".update-count.*")
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	if _, err := fmt.Fprintf(tmp, "%d\n", readInstallCount()+1); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), installCountPath); err != nil {
		return err
	}
	return syncDir(dir)
}

// syncFilesystems makes the switch durable: the files were already fsync'ed in the stage, so
// only directories remain (those under /kvmapp, whose entries the moves created, and the parents
// of /kvmapp and /root/old). A directory that disappears meanwhile (runtime state of the running
// app) is skipped; any other error is returned. A variable so tests can see the order and make
// it fail.
var syncFilesystems = func() error {
	err := filepath.WalkDir(installAppDir, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if !entry.IsDir() {
			return nil
		}
		if err := fsyncPath(path); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("fsync %s: %w", path, err)
		}
		return nil
	})
	if err != nil {
		return err
	}
	for _, dir := range []string{filepath.Dir(installAppDir), filepath.Dir(installBackupDir)} {
		if err := syncDir(dir); err != nil {
			return err
		}
	}
	return nil
}

// serviceLockPath is the lock S95nanokvm takes for every service operation (start/stop/restart).
var (
	serviceLockPath = "/run/nanokvm-service.lock"
	serviceLockWait = 30 * time.Second
)

// lockServiceOperations waits up to 30 s for S95nanokvm's lock and returns its release.
func lockServiceOperations() (func(), error) {
	file, err := os.OpenFile(serviceLockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open the service lock: %w", err)
	}
	deadline := time.Now().Add(serviceLockWait)
	for {
		if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
			return func() { _ = file.Close() }, nil
		}
		if time.Now().After(deadline) {
			_ = file.Close()
			return nil, errors.New("a service operation (start/stop/restart) is running; nothing was changed")
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// runtimeTemp reports files the running device creates and removes on its own: the video
// library's state file (kvm/.state.tmp.*) and the Wi-Fi request that kvm_system consumes
// (kvm/wifi_try_connect). They may vanish while /kvmapp is being moved and are not part of any
// version; one that is still there stays in /kvmapp.
func runtimeTemp(rel string) bool {
	rel = filepath.ToSlash(rel)
	return strings.HasPrefix(rel, "kvm/.state.tmp.") || rel == "kvm/wifi_try_connect"
}

// moveTree moves every file of src into dst like utils.MoveFilesRecursively, but skips runtime
// temporary files, including one that disappears while it is being moved. Any other file that
// cannot be moved is an error.
func moveTree(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		rel, relErr := filepath.Rel(src, path)
		if relErr != nil {
			return relErr
		}
		if runtimeTemp(rel) {
			return nil
		}
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, info.Mode())
		}
		return moveFile(path, target)
	})
}

// moveFile is utils.MoveFile; a variable so tests can interleave with a running app.
var moveFile = utils.MoveFile

// installTrashDir is where the previous backup is renamed to before it is removed; the same place
// as kvmapp/system/update-nanokvm.py uses, on the same filesystem as /root/old.
var installTrashDir = "/root/.kvm-update/old.removing"

func removePreviousBackup() error {
	if _, err := os.Lstat(installTrashDir); err == nil {
		if err := os.RemoveAll(installTrashDir); err != nil { // finish a removal cut short
			return err
		}
	}
	if _, err := os.Lstat(installBackupDir); err != nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(installTrashDir), 0o700); err != nil {
		return err
	}
	if err := os.Rename(installBackupDir, installTrashDir); err != nil {
		return err
	}
	return os.RemoveAll(installTrashDir)
}

// fsyncPath opens one file or directory and fsyncs it. A variable so tests can make it fail.
var fsyncPath = func(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	syncErr := f.Sync()
	closeErr := f.Close()
	if syncErr != nil {
		return syncErr
	}
	return closeErr
}

func fsyncTree(root string) error {
	return filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if err := fsyncPath(path); err != nil {
			return fmt.Errorf("fsync %s: %w", path, err)
		}
		return nil
	})
}

// listFiles lists the regular files under dir, relative to it.
func listFiles(dir string) (map[string]struct{}, error) {
	files := make(map[string]struct{})
	err := filepath.WalkDir(dir, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() {
			rel, _ := filepath.Rel(dir, path)
			if !runtimeTemp(rel) {
				files[rel] = struct{}{}
			}
		}
		return nil
	})
	return files, err
}

// restoreExactly makes dir hold exactly the files listed (after a restore from the backup, files
// the new version had already put there, such as kvm_new_app, are removed) and reports whether
// it now does.
func restoreExactly(dir string, want map[string]struct{}) bool {
	have, err := listFiles(dir)
	if err != nil {
		return false
	}
	for rel := range have {
		if _, ok := want[rel]; !ok {
			if err := os.Remove(filepath.Join(dir, rel)); err != nil {
				return false
			}
		}
	}
	for rel := range want {
		if _, ok := have[rel]; !ok {
			return false
		}
	}
	return true
}

// removeIfOnlyDirs removes dir when it holds nothing but (empty) directories and reports whether
// dir is gone. Anything else in it is kept.
func removeIfOnlyDirs(dir string) bool {
	onlyDirs := true
	_ = filepath.WalkDir(dir, func(_ string, entry os.DirEntry, err error) error {
		if err != nil || !entry.IsDir() {
			onlyDirs = false
			return filepath.SkipAll
		}
		return nil
	})
	if !onlyDirs {
		return false
	}
	return os.RemoveAll(dir) == nil
}

func backupCurrentApp() error {
	// akkijp fork: the previous backup was removed (via installTrashDir) before the record was written.
	if err := moveTree(installAppDir, installBackupDir); err != nil {
		return fmt.Errorf("failed to backup app: %w", err)
	}

	return nil
}

func applyUpdate(sourceDir string) error {
	if err := utils.MoveFilesRecursively(sourceDir, installAppDir); err != nil {
		// Try to restore backup on failure
		if restoreErr := moveTree(installBackupDir, installAppDir); restoreErr != nil {
			log.Errorf("Failed to restore backup after update failure: %v", restoreErr)
		}
		return fmt.Errorf("failed to move update in place: %w", err)
	}
	return nil
}
