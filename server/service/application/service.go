package application

const (
	// akkijp fork: updates come only from this fork's GitHub Releases, never from cdn.sipeed.com.
	// The preview channel is not published by the fork, so it points at the stable channel.
	StableURL  = "https://github.com/akkijp/NanoKVM/releases/latest/download"
	PreviewURL = StableURL
	// Packages are downloaded from the release tagged with the version named in latest.json,
	// so a newer "latest" release published mid-update cannot swap the file under us.
	ReleaseDownloadURL = "https://github.com/akkijp/NanoKVM/releases/download"

	AppDir    = "/kvmapp"
	BackupDir = "/root/old"
	CacheDir  = "/root/.kvmcache"

	updateWorkspacePrefix = "nanokvm-update-"
	cacheDirMode          = 0o700
	maxPackageSize        = uint64(1 << 30)
	maxExpandedSize       = uint64(2 << 30)
	maxArchiveEntries     = 100_000
	minFreeReserve        = uint64(128 << 20)
	freeReservePercent    = uint64(5)
)

type Service struct{}

func NewService() *Service {
	return &Service{}
}
