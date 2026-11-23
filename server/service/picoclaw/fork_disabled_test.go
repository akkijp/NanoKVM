package picoclaw

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRuntimeInstallAndStartAreDisabled(t *testing.T) {
	s := &Service{}
	if _, err := s.installRuntime(); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("installRuntime must be refused, got %+v", err)
	}
	if _, _, err := s.startRuntimeContext(context.Background()); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("startRuntime must be refused, got %+v", err)
	}
	// Saving a model config onboards first; that must not run the binary either.
	if _, err := runPicoclawOnboardForConfig(); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("onboard must be refused, got %+v", err)
	}
	if _, err := runPicoclawOnboardContext(context.Background()); err == nil || err.Message != runtimeDisabledMessage {
		t.Fatalf("onboard must be refused, got %+v", err)
	}
}

func TestStartAndInstallHandlersRefuseBeforeStateChanges(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for name, handler := range map[string]func(*Service, *gin.Context){
		"start":   (*Service).StartRuntime,
		"install": (*Service).InstallRuntime,
	} {
		s := &Service{}
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/api/picoclaw/runtime/"+name, nil)
		handler(s, c)
		if !strings.Contains(w.Body.String(), runtimeDisabledMessage) {
			t.Fatalf("%s: response %q does not say the runtime is disabled", name, w.Body.String())
		}
		if status := s.runtime.Get(); status.Status == "starting" || status.Installing {
			t.Fatalf("%s: runtime state changed to %+v", name, status)
		}
	}
}
