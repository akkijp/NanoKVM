package application

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"

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

	// Complete: only the restart is missing, which may now be retried.
	record.Phase = phaseInstalled
	if err := writePending(record); err != nil {
		return err
	}
	return nil
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
			files[rel] = struct{}{}
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
	if err := utils.MoveFilesRecursively(installAppDir, installBackupDir); err != nil {
		return fmt.Errorf("failed to backup app: %w", err)
	}

	return nil
}

func applyUpdate(sourceDir string) error {
	if err := utils.MoveFilesRecursively(sourceDir, installAppDir); err != nil {
		// Try to restore backup on failure
		if restoreErr := utils.MoveFilesRecursively(installBackupDir, installAppDir); restoreErr != nil {
			log.Errorf("Failed to restore backup after update failure: %v", restoreErr)
		}
		return fmt.Errorf("failed to move update in place: %w", err)
	}
	return nil
}
