package application

import (
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
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
	if refuseWhilePending(c) {
		releaseUpdateLock()
		return
	}

	// akkijp fork: the version the user saw on the page; the update installs only that one.
	expected, err := requestedVersion(c)
	if err != nil {
		releaseUpdateLock()
		rsp.ErrRsp(c, -1, err.Error())
		return
	}
	if err := update(expected); err != nil {
		releaseUpdateLock()
		if errors.Is(err, errLatestChanged) {
			rsp.ErrRsp(c, -5, err.Error())
			return
		}
		rsp.ErrRsp(c, -1, fmt.Sprintf("update failed: %s", err))
		return
	}

	setRestartUnderWay(true)
	rsp.OkRsp(c)
	log.Debugf("update application success")

	startRestart()
}

var (
	restartCommand = []string{"/kvmapp/system/init.d/S95nanokvm", "restart"}
	restartDelay   = 1 * time.Second
)

// restarts counts the restarts started in the background, so tests can wait for them.
var restarts sync.WaitGroup

func startRestart() {
	restarts.Add(1)
	go func() {
		defer restarts.Done()
		restartServices()
	}()
}

func restartServices() {
	// Let the HTTP response reach the client before stopping the server.
	time.Sleep(restartDelay)

	if err := exec.Command(restartCommand[0], restartCommand[1:]...).Run(); err != nil {
		log.Errorf("failed to restart services after update: %v", err)
		setRestartUnderWay(false) // the UI now offers to retry
		// akkijp fork: this process keeps running, so let other updates (including the
		// maintenance updater, which shares the lock file) proceed; they retry the restart.
		releaseUpdateLock()
	}
}

var errLatestChanged = errors.New("the latest version changed")

const maxUpdateRequest = 4096

// requestedVersion reads the version the page asks for. Only an empty body (an older page) means
// "whatever is latest"; a body that cannot be parsed, or lacks a valid version, is refused before
// anything is downloaded, so a truncated request never installs a version nobody saw.
func requestedVersion(c *gin.Context) (string, error) {
	body, err := io.ReadAll(io.LimitReader(c.Request.Body, maxUpdateRequest+1))
	if err != nil {
		return "", fmt.Errorf("invalid update request: %w", err)
	}
	if len(body) > maxUpdateRequest {
		return "", errors.New("invalid update request: too large")
	}
	if len(body) == 0 {
		return "", nil // an older page: no body at all
	}
	var req struct {
		Version *string `json:"version"`
	}
	if err := json.Unmarshal(body, &req); err != nil || req.Version == nil || !versionPattern.MatchString(*req.Version) {
		return "", errors.New("invalid update request: a version is required")
	}
	return *req.Version, nil
}

func update(expected string) error {
	latest, err := getLatest()
	if err != nil {
		return err
	}
	if expected != "" && latest.Version != expected {
		return fmt.Errorf("%w: %s is now the latest, not %s; check again", errLatestChanged, latest.Version, expected)
	}
	if err := prepareCacheForUpdate(); err != nil {
		return err
	}
	workspace, err := newUpdateWorkspace(installCacheDir)
	if err != nil {
		return err
	}
	defer func() {
		if err := workspace.Close(); err != nil {
			log.Warnf("failed to clean update workspace %s: %v", workspace.dir, err)
		}
	}()
	if err := ensureInstallFilesystem(workspace.dir, installAppDir, installBackupDir); err != nil {
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
