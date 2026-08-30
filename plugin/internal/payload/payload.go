// Package payload models the hook events the harness writes to our stdin.
//
// Every field here is observed in testdata/golden, not taken from a spec — see
// testdata/golden/README.md for the six places the specs disagree with reality.
// The most important: a *failed* tool call emits PreToolUse and no PostToolUse,
// so error status is derived from an unclosed step, never from a result field.
//
// This is the only Claude-Code-specific package. The Codex adapter (milestone 6)
// lands here as a second decoder producing the same structs — Codex's turn_id maps
// onto PromptID and nothing downstream changes.
package payload

import (
	"encoding/json"
	"fmt"
)

// Event is the common envelope. Raw keeps the undecoded object so a field we
// don't model yet is still available to the digest without a schema bump.
type Event struct {
	HookEventName string `json:"hook_event_name"`
	SessionID     string `json:"session_id"`
	Cwd           string `json:"cwd"`
	// Present on UserPromptSubmit, PreToolUse, PostToolUse, Stop and SessionEnd.
	// This is the segmentation key: one run = all steps sharing one PromptID.
	PromptID string `json:"prompt_id"`

	// SessionStart
	Source string `json:"source"`

	// UserPromptSubmit
	Prompt string `json:"prompt"`

	// Pre/PostToolUse
	ToolName  string          `json:"tool_name"`
	ToolUseID string          `json:"tool_use_id"`
	ToolInput json.RawMessage `json:"tool_input"`
	// NOT tool_result. PLUGIN_BUILD.md's "correction #1" is wrong; the observed
	// key is tool_response, as AGENT_HOOK_SHIM.md originally said.
	ToolResponse json.RawMessage `json:"tool_response"`
	DurationMs   int             `json:"duration_ms"`

	// Stop. There is no stop_reason field; StopHookActive is a re-entrancy guard
	// (true when a Stop hook is already running), not an outcome signal.
	LastAssistantMessage string `json:"last_assistant_message"`
	StopHookActive       bool   `json:"stop_hook_active"`

	// SessionEnd. The key is "reason", not "session_end_reason".
	Reason string `json:"reason"`

	Raw map[string]any `json:"-"`
}

// Decode parses one hook payload. A decode failure is an error the caller logs
// and swallows — it must never become a non-zero exit.
func Decode(b []byte) (*Event, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("empty stdin")
	}
	var e Event
	if err := json.Unmarshal(b, &e); err != nil {
		return nil, fmt.Errorf("decode hook payload: %w", err)
	}
	_ = json.Unmarshal(b, &e.Raw)
	if e.SessionID == "" {
		return nil, fmt.Errorf("hook payload has no session_id")
	}
	return &e, nil
}

// ToolInputMap decodes tool_input, which every tool shapes differently.
func (e *Event) ToolInputMap() map[string]any {
	if len(e.ToolInput) == 0 {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal(e.ToolInput, &m); err != nil {
		return nil
	}
	return m
}

// StringField pulls a string out of tool_input.
func (e *Event) StringField(key string) string {
	m := e.ToolInputMap()
	if m == nil {
		return ""
	}
	if s, ok := m[key].(string); ok {
		return s
	}
	return ""
}
