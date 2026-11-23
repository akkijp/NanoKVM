package picoclaw

import (
	"context"
	"testing"
)

func TestRuntimeInstallAndStartAreDisabled(t *testing.T) {
	s := &Service{}
	if _, err := s.installRuntime(); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("installRuntime must be refused, got %+v", err)
	}
	if _, _, err := s.startRuntimeContext(context.Background()); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("startRuntime must be refused, got %+v", err)
	}
}
