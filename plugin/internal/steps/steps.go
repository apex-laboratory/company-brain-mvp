// Package steps maps a hook event onto one envelope step.
//
// The mapping table is PLUGIN_BUILD.md#step-mapping, with paths made relative to
// cwd — an absolute path leaks the developer's username into a corpus that other
// people in the company will read.
package steps

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/brainite/plugin/internal/payload"
	"github.com/brainite/plugin/internal/redact"
)

// Envelope step types. file_write is a Brainite addition to the PRD's original
// four: folding writes into tool_call erases the read/write distinction, which is
// exactly the signal a procedure needs.
const (
	TypeToolCall         = "tool_call"
	TypeFileRead         = "file_read"
	TypeFileWrite        = "file_write"
	TypeShell            = "shell"
	TypeAssistantMessage = "assistant_message"
)

const (
	StatusOK      = "ok"
	StatusError   = "error"
	StatusSkipped = "skipped"
)

var fileReadTools = map[string]bool{"Read": true, "NotebookRead": true}
var fileWriteTools = map[string]bool{"Write": true, "Edit": true, "NotebookEdit": true}
var plainToolCalls = map[string]bool{"Grep": true, "Glob": true, "WebFetch": true, "WebSearch": true}

// Mapped is the type/name/args triple derived from a tool call.
type Mapped struct {
	Type string
	Name string
	Args map[string]any
}

// Map classifies one tool invocation.
func Map(e *payload.Event) Mapped {
	tool := e.ToolName
	switch {
	case fileReadTools[tool]:
		return Mapped{Type: TypeFileRead, Name: relPath(e.StringField("file_path"), e.Cwd)}
	case fileWriteTools[tool]:
		return Mapped{Type: TypeFileWrite, Name: relPath(e.StringField("file_path"), e.Cwd)}
	case tool == "Bash":
		// The command is both the most valuable field in the trace and the most
		// dangerous, so it goes through the value patterns before it is spooled.
		return Mapped{Type: TypeShell, Name: redact.String(e.StringField("command"))}
	case tool == "Task":
		sub := e.StringField("subagent_type")
		if sub == "" {
			sub = "unknown"
		}
		return Mapped{Type: TypeToolCall, Name: "Task:" + sub}
	case strings.HasPrefix(tool, "mcp__"):
		// Keep the full name. Feature 34 reinforcement reads
		// mcp__brainite__query_brain to tell whether the agent consulted the brain.
		return Mapped{Type: TypeToolCall, Name: tool, Args: safeArgs(e)}
	case plainToolCalls[tool]:
		return Mapped{Type: TypeToolCall, Name: tool, Args: safeArgs(e)}
	default:
		return Mapped{Type: TypeToolCall, Name: tool, Args: safeArgs(e)}
	}
}

// safeArgs redacts tool_input for the step types that keep their arguments. File
// and shell steps carry everything they need in Name, so they keep no args at all.
func safeArgs(e *payload.Event) map[string]any {
	m := e.ToolInputMap()
	if m == nil {
		return nil
	}
	red, ok := redact.Value(m).(map[string]any)
	if !ok {
		return nil
	}
	return red
}

func relPath(p, cwd string) string {
	if p == "" {
		return ""
	}
	if cwd == "" {
		return filepath.Base(p)
	}
	rel, err := filepath.Rel(cwd, p)
	if err != nil || strings.HasPrefix(rel, "..") {
		// Outside the repo: keep the basename only rather than an absolute path
		// that would carry /Users/<name>/ into the corpus.
		return filepath.Base(p)
	}
	return rel
}

// Digest summarises tool_response without storing it. The raw response holds file
// contents, shell output and customer records; only its fingerprint and size ever
// reach the spool.
func Digest(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	sum := sha256.Sum256(raw)
	body := string(raw)
	lines := strings.Count(body, "\n") + 1
	return fmt.Sprintf("%s bytes=%d lines=%d", hex.EncodeToString(sum[:])[:16], len(raw), lines)
}

// StatusOf reads success or failure out of a tool_response.
//
// A *failed* tool call emits no PostToolUse at all (see
// testdata/golden/README.md), so this only ever sees survivors — the caller
// derives error from a step that was opened and never closed. What this catches
// is the narrower case of a tool that returned successfully while reporting
// failure inside its payload: a shell command with a non-empty stderr, or an
// interrupted Bash call.
func StatusOf(raw json.RawMessage) string {
	if len(raw) == 0 {
		return StatusOK
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return StatusOK
	}
	if b, ok := m["interrupted"].(bool); ok && b {
		return StatusError
	}
	if s, ok := m["stderr"].(string); ok && strings.TrimSpace(s) != "" {
		return StatusError
	}
	if s, ok := m["error"].(string); ok && strings.TrimSpace(s) != "" {
		return StatusError
	}
	return StatusOK
}
