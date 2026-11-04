package api

// 계약(contracts/openapi.yaml)의 응답 타입 중 에이전트가 쓰는 것.

type PartURL struct {
	PartNumber int    `json:"part_number"`
	URL        string `json:"url"`
	ExpiresAt  string `json:"expires_at"`
}

type UploadCreated struct {
	Status    string    `json:"status"`
	UploadID  string    `json:"upload_id"`
	AssetID   string    `json:"asset_id"`
	PartSize  int64     `json:"part_size"`
	PartCount int       `json:"part_count"`
	Parts     []PartURL `json:"parts"`
	ExpiresAt string    `json:"expires_at"`
}

type CompletedPart struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
	Size       int64  `json:"size,omitempty"`
}

type PartList struct {
	UploadID       string          `json:"upload_id"`
	PartSize       int64           `json:"part_size"`
	PartCount      int             `json:"part_count"`
	CompletedParts []CompletedPart `json:"completed_parts"`
	PendingParts   []PartURL       `json:"pending_parts"`
}

type UploadStatus struct {
	UploadID         string `json:"upload_id"`
	AssetID          string `json:"asset_id"`
	IngestStatus     string `json:"ingest_status"`
	QuarantineReason string `json:"quarantine_reason"`
}

type Source struct {
	DeviceID         string   `json:"device_id"`
	ProjectID        string   `json:"project_id"`
	CollectionMode   string   `json:"collection_mode"`
	LocalCopyPolicy  string   `json:"local_copy_policy"`
	AllowedDataRoles []string `json:"allowed_data_roles"`
	SourceTimezone   *string  `json:"source_timezone"`
}

type WatchRule struct {
	RuleID          string   `json:"rule_id"`
	SourceDeviceID  string   `json:"source_device_id"`
	PathPattern     string   `json:"path_pattern"`
	Include         []string `json:"include"`
	Exclude         []string `json:"exclude"`
	DataType        string   `json:"data_type"`
	Variant         string   `json:"variant"`
	DataRoleDefault string   `json:"data_role_default"`
	Priority        int      `json:"priority"`
}

type TransferWindow struct {
	Days       []string `json:"days"`
	StartLocal string   `json:"start_local"`
	EndLocal   string   `json:"end_local"`
	MaxKbps    *int     `json:"max_kbps"`
}

type Policy struct {
	PolicyVersion      string           `json:"policy_version"`
	CollectorDeviceID  string           `json:"collector_device_id"`
	Sources            []Source         `json:"sources"`
	WatchRules         []WatchRule      `json:"watch_rules"`
	TransferWindows    []TransferWindow `json:"transfer_windows"`
	Timezone           string           `json:"timezone"`
	HeartbeatIntervalS int              `json:"heartbeat_interval_s"`
	SizeStableS        int              `json:"size_stable_s"`
	MaxParallelParts   int              `json:"max_parallel_parts"`
}

func (p *Policy) Source(id string) *Source {
	for i := range p.Sources {
		if p.Sources[i].DeviceID == id {
			return &p.Sources[i]
		}
	}
	return nil
}
