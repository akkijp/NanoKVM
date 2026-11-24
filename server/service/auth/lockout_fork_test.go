package auth

import (
	"testing"

	"NanoKVM-Server/config"
)

// akkijp fork: after 5 failures from one address, that address is refused even with the right
// password (CheckLoginAttempt runs before the password is checked), and other addresses are not.
func TestFiveFailuresLockTheAddressOut(t *testing.T) {
	conf := config.GetInstance()
	old := conf.Security
	t.Cleanup(func() {
		conf.Security = old
		loginMutex.Lock()
		loginAttempts = make(map[string]*loginAttempt)
		loginMutex.Unlock()
	})
	conf.Security.LoginLockoutDuration, conf.Security.LoginMaxFailures = 900, 5
	ip := "192.0.2.10"
	for i := 1; i <= 4; i++ {
		RecordLoginFailure(ip)
		if locked, _, _ := CheckLoginAttempt(ip); locked {
			t.Fatalf("locked after %d failures", i)
		}
	}
	RecordLoginFailure(ip)
	if locked, code, _ := CheckLoginAttempt(ip); !locked || code != -5 {
		t.Fatalf("not locked after 5 failures (code %d)", code)
	}
	if locked, _, _ := CheckLoginAttempt("192.0.2.11"); locked {
		t.Fatal("another address must not be locked")
	}
}
