package application

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"NanoKVM-Server/proto"

	"github.com/gin-gonic/gin"
	log "github.com/sirupsen/logrus"
)

// akkijp fork: the record of an update that has not been seen running yet.
//
// It is written (and synced) before /kvmapp is touched and kept until the new version has been
// seen running: S95nanokvm removes it once both daemons have stayed up, and
// kvmapp/system/update-nanokvm.py removes it once it sees the version it started, or the
// previous version, running again. While it exists /root/old is the only version known to work,
// so neither this server nor update-nanokvm.py installs anything.
//
//	phase "switching": /kvmapp is being replaced; its contents may be half old, half new.
//	                   Restarting would run whatever is there, so only manual recovery (FORK.md).
//	phase "installed": the new version is complete in /kvmapp; only the restart is missing,
//	                   which POST /api/application/update/restart retries.
//
// It lives on disk so that a reboot or a killed process does not lose it. The same file and
// format are used by kvmapp/system/update-nanokvm.py.
var (
	restartPendingPath = "/etc/kvm/update-restart-pending"
	microphoneStatus   = "/run/nanokvm-microphone" // written by S95nanokvm

	// processStart tells a server started by the restart (after the record was last written)
	// from the old one still running because the restart failed.
	processStart    = time.Now()
	confirmDeadline = 60 * time.Second
)

const (
	phaseSwitching = "switching"
	phaseInstalled = "installed"
	// Reported only: an installed update whose new server is running while S95nanokvm waits
	// for both daemons to stay up before removing the record.
	phaseConfirming = "confirming"
	// Reported only: "switching" while an update holds the update lock.
	phaseUpdating = "updating"
	// Reported only: "installed" while this server has a restart under way (it has not failed).
	phaseRestarting = "restarting"
)

// restartUnderWay is set from the moment an update or retry has been accepted until the restart
// command fails; a successful restart ends this process, and with it the flag.
var (
	restartUnderWayMu sync.Mutex
	restartUnderWay   bool
)

func setRestartUnderWay(v bool) {
	restartUnderWayMu.Lock()
	restartUnderWay = v
	restartUnderWayMu.Unlock()
}

func isRestartUnderWay() bool {
	restartUnderWayMu.Lock()
	defer restartUnderWayMu.Unlock()
	return restartUnderWay
}

// updateInProgress reports whether an update (in this process, or update-nanokvm.py, which
// shares the lock file) is running right now.
func updateInProgress() bool {
	mutex.Lock()
	busy := isUpdating
	mutex.Unlock()
	if busy {
		return true
	}
	file, err := os.OpenFile(updateLockPath, os.O_RDONLY, 0)
	if err != nil {
		return false
	}
	defer file.Close()
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return true
	}
	_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	return false
}

// reportedPhase is the phase to show: "confirming" when this server started after the record
// was written (so the restart happened), otherwise the recorded phase.
func reportedPhase(record pendingRecord) string {
	if record.Phase == phaseSwitching && updateInProgress() {
		return phaseUpdating // an update is replacing /kvmapp right now; not a stopped one
	}
	if record.Phase != phaseInstalled {
		return record.Phase
	}
	// S95nanokvm clears the record 15 s after starting both daemons. If it is still there well
	// after this server started, a daemon did not stay up: the restart may be retried.
	if info, err := os.Stat(restartPendingPath); err == nil && processStart.After(info.ModTime()) &&
		time.Since(processStart) < confirmDeadline {
		return phaseConfirming
	}
	if isRestartUnderWay() {
		return phaseRestarting // this (old) server is about to be restarted
	}
	return phaseInstalled // the restart failed: it can be retried
}

func readMicrophoneStatus() string {
	data, err := os.ReadFile(microphoneStatus)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

type pendingRecord struct {
	From  string `json:"from"`
	To    string `json:"to"`
	Phase string `json:"phase"`
}

// readPending reports the record, if any. An unreadable record counts as "switching": the state
// of /kvmapp is then unknown, and that is the case that must not be restarted blindly.
func readPending() (pendingRecord, bool) {
	data, err := os.ReadFile(restartPendingPath)
	if os.IsNotExist(err) {
		return pendingRecord{}, false
	}
	var record pendingRecord
	if err != nil || json.Unmarshal(data, &record) != nil || record.Phase != phaseInstalled {
		record.Phase = phaseSwitching
	}
	return record, true
}

func isRestartPending() bool {
	_, pending := readPending()
	return pending
}

// writePending replaces the record atomically: a temporary file in the same directory is written
// in full and synced, renamed over the record, and the directory is synced. A failure at any
// point leaves the previous record (or none) in place, never a partial one.
func writePending(record pendingRecord) error {
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	dir := filepath.Dir(restartPendingPath)
	tmp, err := os.CreateTemp(dir, ".update-restart-pending.*")
	if err != nil {
		return fmt.Errorf("record the pending restart: %w", err)
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()
	if err := tmp.Chmod(0o644); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("record the pending restart: %w", err)
	}
	if _, err := tmp.Write(append(data, '\n')); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("record the pending restart: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("record the pending restart: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("record the pending restart: %w", err)
	}
	if err := os.Rename(tmpPath, restartPendingPath); err != nil {
		return fmt.Errorf("record the pending restart: %w", err)
	}
	if err := syncDir(dir); err != nil {
		return fmt.Errorf("record the pending restart: %w", err)
	}
	return nil
}

// syncDir makes a rename in dir durable. A variable so tests can make it fail.
var syncDir = func(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	syncErr := d.Sync()
	closeErr := d.Close()
	if syncErr != nil {
		return syncErr
	}
	return closeErr
}

func clearPending() error {
	if err := os.Remove(restartPendingPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// readVersionFileStrict tells a missing version file ("", nil) from one that cannot be read.
func readVersionFileStrict(dir string) (string, error) {
	data, err := os.ReadFile(dir + "/version")
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

func readVersionFile(dir string) string {
	data, err := os.ReadFile(dir + "/version")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// refuseWhilePending answers an update request while an earlier update is unfinished: nothing is
// installed, and the response says how to finish it. It reports whether it answered.
func refuseWhilePending(c *gin.Context) bool {
	record, pending := readPending()
	if !pending {
		return false
	}
	var rsp proto.Response
	if record.Phase == phaseInstalled {
		rsp.ErrRsp(c, -3, fmt.Sprintf("version %s is installed but not running yet; retry the restart first (nothing was installed)", record.To))
	} else {
		rsp.ErrRsp(c, -3, "an earlier update stopped while replacing /kvmapp; recover as described in FORK.md (nothing was installed)")
	}
	return true
}

// RetryRestart restarts into an installed update that is waiting for its restart. It does
// nothing else: it installs nothing, and without a waiting update it just reports that.
func (s *Service) RetryRestart(c *gin.Context) {
	var rsp proto.Response

	if !acquireUpdateLock() {
		rsp.ErrRsp(c, -1, "update already in progress")
		return
	}
	record, pending := readPending()
	switch {
	case !pending:
		releaseUpdateLock()
		rsp.OkRspWithData(c, gin.H{"restarting": false})
	case record.Phase != phaseInstalled:
		releaseUpdateLock()
		rsp.ErrRsp(c, -3, "the earlier update stopped while replacing /kvmapp; restarting would run an incomplete app. Recover as described in FORK.md")
	case reportedPhase(record) == phaseConfirming:
		// This server is the restarted one; S95nanokvm is only confirming it. Nothing to retry.
		releaseUpdateLock()
		rsp.OkRspWithData(c, gin.H{"restarting": false})
	default:
		log.Infof("retrying the restart into %s", record.To)
		setRestartUnderWay(true)
		rsp.OkRspWithData(c, gin.H{"restarting": true})
		go restartServices()
	}
}
