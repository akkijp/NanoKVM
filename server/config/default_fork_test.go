package config

import "testing"

// akkijp fork: lockout cannot be disabled through the config file.
// The fork's lockout: 5 failures from one address lock it out for 15 minutes. Checked against
// the numbers themselves, so an upstream merge that changes the constants fails here.
func TestLockoutDefaultsAreTheForkValues(t *testing.T) {
	if DefaultLoginLockoutDuration != 900 || DefaultLoginMaxFailures != 5 {
		t.Fatalf("lockout defaults %d s / %d failures, want 900 s / 5", DefaultLoginLockoutDuration, DefaultLoginMaxFailures)
	}
	instance = Config{}
	instance.JWT.SecretKey = "x"
	checkDefaultValue()
	if instance.Security.LoginLockoutDuration != 900 || instance.Security.LoginMaxFailures != 5 {
		t.Fatalf("an empty config gives %+v", instance.Security)
	}
}

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
