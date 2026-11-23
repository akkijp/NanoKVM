package proto

type GetVersionRsp struct {
	Current string `json:"current"`
	Latest  string `json:"latest"`
	// akkijp fork: an update has not been seen running yet (see application/pending.go).
	RestartPending bool   `json:"restartPending,omitempty"`
	RestartPhase   string `json:"restartPhase,omitempty"`
	PendingVersion string `json:"pendingVersion,omitempty"`
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
