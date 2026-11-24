package auth

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"NanoKVM-Server/authn"
	"NanoKVM-Server/config"

	"github.com/gin-gonic/gin"
)

// akkijp fork: through the login handler, 5 wrong passwords from one address lock it out: the
// right password is then refused with -5 and no session cookie, while another address can
// still log in.
func TestFiveFailuresLockTheAddressOut(t *testing.T) {
	gin.SetMode(gin.TestMode)
	conf := config.GetInstance()
	old := conf.Security
	t.Cleanup(func() {
		conf.Security = old
		loginMutex.Lock()
		loginAttempts = make(map[string]*loginAttempt)
		loginMutex.Unlock()
	})
	conf.Security.LoginLockoutDuration, conf.Security.LoginMaxFailures = 900, 5
	store := authn.NewStore(filepath.Join(t.TempDir(), "pwd"))
	restore := useTestStore(store)
	defer restore()
	service := NewService()
	router := gin.New()
	router.POST("/login", service.Login)

	login := func(remote, password string) (string, []*http.Cookie) {
		req := jsonRequest(http.MethodPost, "/login", map[string]any{"username": "admin", "password": encryptForRequest(password)})
		req.RemoteAddr = remote
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		return w.Body.String(), w.Result().Cookies()
	}
	for i := 1; i <= 5; i++ {
		if body, _ := login("192.0.2.10:1000", "wrong"); !strings.Contains(body, `"code":-2`) {
			t.Fatalf("failure %d: %s", i, body)
		}
	}
	body, cookies := login("192.0.2.10:1001", "admin")
	if !strings.Contains(body, `"code":-5`) {
		t.Fatalf("the right password after 5 failures: %s", body)
	}
	for _, cookie := range cookies {
		if cookie.Value != "" {
			t.Fatalf("a session cookie was issued while locked out: %s", cookie.Name)
		}
	}
	if body, cookies := login("192.0.2.11:1000", "admin"); !strings.Contains(body, `"code":0`) || len(cookies) == 0 {
		t.Fatalf("another address must still log in: %s", body)
	}
}
