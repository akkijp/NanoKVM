package picoclaw

// akkijp fork: the AI agent runtime (picoclaw) is disabled. It would hand keyboard, mouse and
// screen to an LLM, and its binary comes from cdn.sipeed.com with a checksum served by the same
// CDN. Installing and starting the runtime are refused; the rest of the service stays so the
// web UI keeps working.
const runtimeDisabledByFork = true

const runtimeDisabledMessage = "the AI agent (picoclaw) is disabled in this build (akkijp fork)"

// ControlModeRefused tells why a switch of the AI control mode to mode is refused in this build:
// the picoclaw mode would take control away from MCP (cancelling its operations) and hand it to
// a runtime that cannot start. Empty when the switch is allowed.
func ControlModeRefused(mode string) string {
	if runtimeDisabledByFork && mode == "picoclaw" {
		return runtimeDisabledMessage
	}
	return ""
}
