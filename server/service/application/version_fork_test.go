package application

import (
	"strings"
	"testing"
)

// akkijp fork: X.Y.Z.N versions must be accepted end to end.
func TestValidateLatestAcceptsFourPartVersion(t *testing.T) {
	latest := validLatest()
	latest.Version, latest.Name = "2.5.4.1", "nanokvm_2.5.4.1.tar.gz"
	if err := validateLatest(&latest); err != nil {
		t.Fatalf("four-part version rejected: %v", err)
	}
}

func TestValidateLatestRejectsMalformedVersions(t *testing.T) {
	for _, v := range []string{"2.5.4.1.2", "2.5", "2.5.4.", "2.5.4.x", "../2.5.4"} {
		latest := validLatest()
		latest.Version = v
		if err := validateLatest(&latest); err == nil {
			t.Errorf("version %q must be rejected", v)
		}
	}
	for _, n := range []string{"nanokvm_2.5.4.1.2.tar.gz", "nanokvm_2.5.4.x.tar.gz", "nanokvm_2.5.4.1.zip"} {
		latest := validLatest()
		latest.Name = n
		if err := validateLatest(&latest); err == nil {
			t.Errorf("package name %q must be rejected", n)
		}
	}
}

func TestUpdateURLsPointAtFork(t *testing.T) {
	for _, u := range []string{StableURL, PreviewURL} {
		if !strings.HasPrefix(u, "https://github.com/akkijp/NanoKVM/") || strings.Contains(u, "sipeed.com") {
			t.Errorf("update URL %q must point at the akkijp fork", u)
		}
	}
}
