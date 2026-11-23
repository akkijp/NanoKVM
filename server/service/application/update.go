package application

import (
	"crypto/sha512"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	log "github.com/sirupsen/logrus"

	"NanoKVM-Server/proto"
	"NanoKVM-Server/utils"
)

const (
	maxTries = 3
)

func (s *Service) Update(c *gin.Context) {
	var rsp proto.Response

	if !acquireUpdateLock() {
		rsp.ErrRsp(c, -1, "update already in progress")
		return
	}
	if retryPendingRestart(c) {
		return
	}

	if err := update(); err != nil {
		releaseUpdateLock()
		rsp.ErrRsp(c, -1, fmt.Sprintf("update failed: %s", err))
		return
	}

	rsp.OkRsp(c)
	log.Debugf("update application success")

	go restartServices()
}

var (
	restartCommand = []string{"/kvmapp/system/init.d/S95nanokvm", "restart"}
	restartDelay   = 1 * time.Second

	// akkijp fork: exists while an update is in /kvmapp but the restart into it has not happened,
	// so an old process is still the one running. /kvmapp/version then names a version that is not
	// running, and /root/old holds the only version known to work: neither this server nor
	// kvmapp/system/update-nanokvm.py may install anything until the restart has happened.
	// S95nanokvm removes it once it has started the daemons; /run is empty after a reboot.
	restartPendingPath = "/run/nanokvm-restart-pending"
)

func isRestartPending() bool {
	_, err := os.Stat(restartPendingPath)
	return err == nil
}

func setRestartPending(pending bool) {
	if !pending {
		_ = os.Remove(restartPendingPath)
		return
	}
	if err := os.WriteFile(restartPendingPath, []byte("restart pending\n"), 0o644); err != nil {
		log.Errorf("failed to record the pending restart: %v", err)
	}
}

func restartServices() {
	// Let the HTTP response reach the client before stopping the server.
	time.Sleep(restartDelay)

	setRestartPending(true) // cleared only by the process dying, i.e. a successful restart
	if err := exec.Command(restartCommand[0], restartCommand[1:]...).Run(); err != nil {
		log.Errorf("failed to restart services after update: %v", err)
		// akkijp fork: this process keeps running, so let other updates (including the
		// maintenance updater, which shares the lock file) proceed; they retry the restart.
		releaseUpdateLock()
	}
}

// retryPendingRestart answers an update request while a previous update waits for its restart:
// it retries only the restart and installs nothing. It reports whether it handled the request.
func retryPendingRestart(c *gin.Context) bool {
	if !isRestartPending() {
		return false
	}
	var rsp proto.Response
	rsp.ErrRsp(c, -2, "the previous update is installed but not running yet; restarting it now, try again afterwards")
	go restartServices()
	return true
}

func update() error {
	latest, err := getLatest()
	if err != nil {
		return err
	}
	if err := prepareCacheForUpdate(); err != nil {
		return err
	}
	workspace, err := newUpdateWorkspace(CacheDir)
	if err != nil {
		return err
	}
	defer func() {
		if err := workspace.Close(); err != nil {
			log.Warnf("failed to clean update workspace %s: %v", workspace.dir, err)
		}
	}()
	if err := ensureInstallFilesystem(workspace.dir, AppDir, BackupDir); err != nil {
		return err
	}
	if err := preflightManifestSpace(workspace.dir, latest); err != nil {
		return err
	}

	target := filepath.Join(workspace.dir, latest.Name)
	downloadInfo, err := download(latest, target)
	if err != nil {
		log.Errorf("download app failed: %s", err)
		return err
	}
	if err := validateDownloadedSize(latest, uint64(downloadInfo.Written)); err != nil {
		return err
	}

	if err := checksum(target, latest.Sha512); err != nil {
		log.Errorf("check sha512 failed: %s", err)
		return err
	}
	expectedRoot := strings.TrimSuffix(latest.Name, ".tar.gz")
	info, err := inspectUpdateArchive(target, expectedRoot)
	if err != nil {
		return fmt.Errorf("inspect update package: %w", err)
	}
	if err := validateExpandedSize(latest, info.expandedBytes); err != nil {
		return err
	}
	if err := ensureExpandedSpace(workspace.dir, info.expandedBytes); err != nil {
		return err
	}
	sourceDir, err := extractUpdateArchive(target, workspace.dir, expectedRoot)
	if err != nil {
		return fmt.Errorf("extract update package: %w", err)
	}
	if err := validateExtractedPackage(sourceDir, latest.Version); err != nil {
		return err
	}
	if err := installPreparedPackage(sourceDir); err != nil {
		log.Errorf("failed to install package: %v", err)
		return err
	}

	return nil
}

func download(latest *Latest, target string) (info utils.DownloadInfo, err error) {
	for i := range maxTries {
		log.Debugf("attempt #%d/%d", i+1, maxTries)
		if i > 0 {
			time.Sleep(time.Second * 3)
		}

		var req *http.Request
		req, err = utils.NewAuthenticatedRequest("GET", latest.Url, nil)
		if err != nil {
			log.Errorf("new request err: %s", err)
			continue
		}

		log.Debugf("update will be saved to: %s", target)
		info, err = utils.Download(req, target, int64(maxPackageSize), func(contentLength int64) error {
			if latest.ManifestVersion == 2 && uint64(contentLength) != latest.SizeBytes {
				return fmt.Errorf("update package size mismatch: manifest has %d bytes, response has %d", latest.SizeBytes, contentLength)
			}
			return ensureFreeSpace(filepath.Dir(target), uint64(contentLength))
		})
		if err != nil {
			log.Errorf("downloading latest application failed, try again...")
			continue
		}
		return info, nil
	}
	return utils.DownloadInfo{}, err
}

func checksum(filePath string, expectedHash string) error {
	file, err := os.Open(filePath)
	if err != nil {
		log.Errorf("failed to open file %s: %v", filePath, err)
		return err
	}
	defer func() {
		_ = file.Close()
	}()

	hasher := sha512.New()

	_, err = io.Copy(hasher, file)
	if err != nil {
		log.Errorf("failed to copy file contents to hasher: %v", err)
		return err
	}

	hash := base64.StdEncoding.EncodeToString(hasher.Sum(nil))

	if hash != expectedHash {
		log.Errorf("invalid sha512 %s", hash)
		return fmt.Errorf("invalid sha512 %s", hash)
	}

	return nil
}
