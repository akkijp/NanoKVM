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
	record := pendingRecord{From: readVersionFile(installAppDir), To: readVersionFile(sourceDir), Phase: phaseSwitching}
	// The previous backup goes first: if it cannot be removed, /kvmapp is untouched and no
	// "switching" record may be left to block the next boot.
	if err := os.RemoveAll(installBackupDir); err != nil {
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
		if record.From != "" && removeIfOnlyDirs(installBackupDir) && restoreExactly(installAppDir, oldFiles) {
			_ = clearPending() // the old version is in place again, file for file
		}
		return err
	}

	if err := utils.ChmodRecursively(installAppDir, 0o755); err != nil {
		return fmt.Errorf("failed to chmod: %w", err)
	}

	// Complete: only the restart is missing, which may now be retried.
	record.Phase = phaseInstalled
	if err := writePending(record); err != nil {
		return err
	}
	return nil
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
	// akkijp fork: the previous backup was removed before the record was written.
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
