package config

// akkijp fork: login lockout is always on.
const (
	DefaultLoginLockoutDuration = 900
	DefaultLoginMaxFailures     = 5
)

var defaultConfig = &Config{
	Proto: "http",
	Host:  "",
	Port: Port{
		Http:  80,
		Https: 443,
	},
	Cert: Cert{
		Crt: "server.crt",
		Key: "server.key",
	},
	Logger: Logger{
		Level: "info",
		File:  "stdout",
	},
	JWT: JWT{
		SecretKey:            "",
		RefreshTokenDuration: 2678400,
		RevokeTokensOnLogout: true,
	},
	Stun: "stun.l.google.com:19302",
	Turn: Turn{
		TurnAddr: "",
		TurnUser: "",
		TurnCred: "",
	},
	Authentication: "enable",
	Security: Security{
		LoginLockoutDuration: DefaultLoginLockoutDuration,
		LoginMaxFailures:     DefaultLoginMaxFailures,
	},
}

func checkDefaultValue() {
	if instance.JWT.SecretKey == "" {
		instance.JWT.SecretKey = generateRandomSecretKey()
		instance.JWT.RevokeTokensOnLogout = true
	}

	if instance.JWT.RefreshTokenDuration == 0 {
		instance.JWT.RefreshTokenDuration = 2678400
	}

	if instance.Stun == "" {
		instance.Stun = "stun.l.google.com:19302"
	}

	// akkijp fork: brute-force lockout cannot be disabled; 0 or a negative value means the default.
	if instance.Security.LoginLockoutDuration <= 0 {
		instance.Security.LoginLockoutDuration = DefaultLoginLockoutDuration
	}

	if instance.Security.LoginMaxFailures <= 0 {
		instance.Security.LoginMaxFailures = DefaultLoginMaxFailures
	}

	if instance.Authentication == "" {
		instance.Authentication = "enable"
	}

	instance.Hardware = getHardware()
}
