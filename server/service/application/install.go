package application

import (
	"fmt"
	"os"
	"sync"
	"syscall"

	"NanoKVM-Server/utils"
	log "github.com/sirupsen/logrus"
)

var (
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
	// known to work. Record that before touching anything; no record, no switch.
	if err := setRestartPending(true); err != nil {
		return err
	}
	if err := backupCurrentApp(); err != nil {
		return err // the old app may be partly moved: keep the record
	}

	if err := applyUpdate(sourceDir); err != nil {
		if _, statErr := os.Stat(AppDir + "/version"); statErr == nil {
			_ = setRestartPending(false) // the backup was put back; the old version is in place
		}
		return err
	}

	if err := utils.ChmodRecursively(AppDir, 0o755); err != nil {
		return fmt.Errorf("failed to chmod: %w", err)
	}

	return nil
}

func backupCurrentApp() error {
	if err := os.RemoveAll(BackupDir); err != nil {
		return fmt.Errorf("failed to remove backup: %w", err)
	}

	if err := utils.MoveFilesRecursively(AppDir, BackupDir); err != nil {
		return fmt.Errorf("failed to backup app: %w", err)
	}

	return nil
}

func applyUpdate(sourceDir string) error {
	if err := utils.MoveFilesRecursively(sourceDir, AppDir); err != nil {
		// Try to restore backup on failure
		if restoreErr := utils.MoveFilesRecursively(BackupDir, AppDir); restoreErr != nil {
			log.Errorf("Failed to restore backup after update failure: %v", restoreErr)
		}
		return fmt.Errorf("failed to move update in place: %w", err)
	}
	return nil
}
