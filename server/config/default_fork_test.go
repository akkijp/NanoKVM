package config

import "testing"

// akkijp fork: lockout cannot be disabled through the config file.
func TestCheckDefaultValueForcesLockout(t *testing.T) {
	for _, d := range []int{0, -1} {
		instance = Config{Security: Security{LoginLockoutDuration: d, LoginMaxFailures: 0}}
		instance.JWT.SecretKey = "x"
		checkDefaultValue()
		if instance.Security.LoginLockoutDuration != DefaultLoginLockoutDuration {
			t.Errorf("duration %d: got %d", d, instance.Security.LoginLockoutDuration)
		}
		if instance.Security.LoginMaxFailures != DefaultLoginMaxFailures {
			t.Errorf("max failures: got %d", instance.Security.LoginMaxFailures)
		}
	}
	instance = Config{Security: Security{LoginLockoutDuration: 60, LoginMaxFailures: 3}}
	instance.JWT.SecretKey = "x"
	checkDefaultValue()
	if instance.Security.LoginLockoutDuration != 60 || instance.Security.LoginMaxFailures != 3 {
		t.Errorf("explicit values must be kept: %+v", instance.Security)
	}
}
