package proto

type GetVersionRsp struct {
	Current string `json:"current"`
	Latest  string `json:"latest"`
	// akkijp fork: an update has not been seen running yet (see application/pending.go).
	RestartPending bool   `json:"restartPending,omitempty"`
	RestartPhase   string `json:"restartPhase,omitempty"`
	PendingVersion string `json:"pendingVersion,omitempty"`
	// akkijp fork: /run/nanokvm-microphone ("off", "on", or "FAILED: ..."), so a microphone that
	// could not be disabled during an update is shown even though the KVM itself runs.
	Microphone string `json:"microphone,omitempty"`
	// akkijp fork: /run/nanokvm-picoclaw ("off" or "FAILED: ...").
	Picoclaw string `json:"picoclaw,omitempty"`
	// akkijp fork: set when the latest version could not be fetched (the rest is still valid).
	LatestError string `json:"latestError,omitempty"`
	// akkijp fork: when this server process started (ms since the epoch). A page watching an
	// update sees it change when the restart has really happened.
	ServerStarted int64 `json:"serverStarted,omitempty"`
	// akkijp fork: how many updates were installed on this device (/etc/kvm/update-count). A page
	// that reinstalls the running version sees it grow when the install really happened.
	Installs int64 `json:"installs"`
}

type GetPreviewRsp struct {
	Enabled bool `json:"enabled"`
}

type SetPreviewReq struct {
	Enable bool `validate:"omitempty"`
}

type GetUpdateServerRsp struct {
	Enabled bool   `json:"enabled"`
	URL     string `json:"url"`
}

type SetUpdateServerReq struct {
	Enabled *bool  `json:"enabled" form:"enabled" validate:"required"`
	URL     string `json:"url" form:"url"`
}
