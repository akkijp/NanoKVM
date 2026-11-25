package controlmode

import (
	"os"
	"path/filepath"
	"testing"
)

// akkijp fork: picoclaw never holds control in this build.
func TestForkManagerNeverHandsControlToPicoclaw(t *testing.T) {
	dir := t.TempDir()
	// No saved mode (a first install from upstream): off, not picoclaw.
	m := newForkManager(filepath.Join(dir, "none"))
	if got := m.Current(); got != ModeOff {
		t.Fatalf("default mode %q, want off", got)
	}
	// A saved "picoclaw" reads as off.
	saved := filepath.Join(dir, "saved")
	if err := os.WriteFile(saved, []byte("picoclaw\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	m = newForkManager(saved)
	if got := m.Current(); got != ModeOff {
		t.Fatalf("saved picoclaw reads as %q, want off", got)
	}
	// Switching to picoclaw is refused before anything changes; MCP keeps control.
	if err := m.Switch(ModeMCP, nil); err != nil {
		t.Fatal(err)
	}
	called := false
	if err := m.Switch(ModePicoclaw, func() error { called = true; return nil }); err == nil {
		t.Fatal("switching to picoclaw must fail")
	}
	if called || m.Current() != ModeMCP {
		t.Fatalf("the refused switch changed something: preempt=%v mode=%q", called, m.Current())
	}
}
