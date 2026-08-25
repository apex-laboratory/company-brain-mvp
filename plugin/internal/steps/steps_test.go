package steps

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/brainite/plugin/internal/payload"
)

func event(tool, cwd string, input map[string]any) *payload.Event {
	b, _ := json.Marshal(input)
	return &payload.Event{ToolName: tool, Cwd: cwd, ToolInput: b}
}

// TestMappingTable pins PLUGIN_BUILD.md#step-mapping. The read/write distinction
// is the point: "read the policy, then edit the config" and "read the policy,
// then read the config" are different procedures, and folding writes into
// tool_call would erase that.
func TestMappingTable(t *testing.T) {
	cwd := "/repo"
	cases := []struct {
		tool     string
		input    map[string]any
		wantType string
		wantName string
	}{
		{"Read", map[string]any{"file_path": "/repo/app/main.go"}, TypeFileRead, "app/main.go"},
		{"NotebookRead", map[string]any{"file_path": "/repo/nb.ipynb"}, TypeFileRead, "nb.ipynb"},
		{"Write", map[string]any{"file_path": "/repo/out.txt"}, TypeFileWrite, "out.txt"},
		{"Edit", map[string]any{"file_path": "/repo/a/b.go"}, TypeFileWrite, "a/b.go"},
		{"NotebookEdit", map[string]any{"file_path": "/repo/nb.ipynb"}, TypeFileWrite, "nb.ipynb"},
		{"Bash", map[string]any{"command": "make test"}, TypeShell, "make test"},
		{"Grep", map[string]any{"pattern": "x"}, TypeToolCall, "Grep"},
		{"Glob", map[string]any{"pattern": "*.go"}, TypeToolCall, "Glob"},
		{"WebFetch", map[string]any{"url": "https://x"}, TypeToolCall, "WebFetch"},
		{"Task", map[string]any{"subagent_type": "Explore"}, TypeToolCall, "Task:Explore"},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			got := Map(event(tc.tool, cwd, tc.input))
			if got.Type != tc.wantType {
				t.Errorf("type = %q, want %q", got.Type, tc.wantType)
			}
			if got.Name != tc.wantName {
				t.Errorf("name = %q, want %q", got.Name, tc.wantName)
			}
		})
	}
}

// TestQueryBrainKeepsFullName — Feature 34 reinforcement reads this exact name to
// tell whether the agent consulted the brain before acting. Shortening it breaks
// a downstream feature silently.
func TestQueryBrainKeepsFullName(t *testing.T) {
	got := Map(event("mcp__brainite__query_brain", "/repo", map[string]any{"situation": "refunds"}))
	if got.Name != "mcp__brainite__query_brain" {
		t.Errorf("name = %q, want the full MCP tool name", got.Name)
	}
	if got.Type != TypeToolCall {
		t.Errorf("type = %q, want tool_call", got.Type)
	}
}

// TestPathsAreRelative — an absolute path carries the developer's username into a
// corpus the whole company reads.
func TestPathsAreRelative(t *testing.T) {
	got := Map(event("Read", "/Users/someone/work/repo", map[string]any{
		"file_path": "/Users/someone/work/repo/src/x.go",
	}))
	if strings.Contains(got.Name, "someone") || strings.HasPrefix(got.Name, "/") {
		t.Errorf("name = %q leaks an absolute path", got.Name)
	}
	// A file outside the repo keeps only its basename, for the same reason.
	outside := Map(event("Read", "/repo", map[string]any{"file_path": "/Users/someone/.ssh/config"}))
	if strings.Contains(outside.Name, "someone") {
		t.Errorf("out-of-repo path leaked: %q", outside.Name)
	}
}

// TestShellCommandsAreRedacted — the command line is where a pasted credential
// most often ends up.
func TestShellCommandsAreRedacted(t *testing.T) {
	got := Map(event("Bash", "/repo", map[string]any{
		"command": "curl -H 'Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz' https://api",
	}))
	if strings.Contains(got.Name, "sk-ant-api03") {
		t.Errorf("credential survived in the shell command: %q", got.Name)
	}
}

// TestArgsAreRedacted — tool arguments get the same pass as shell commands.
func TestArgsAreRedacted(t *testing.T) {
	got := Map(event("WebFetch", "/repo", map[string]any{
		"url": "https://api.example.com", "apiKey": "plainvalue", "note": "ghp_abcdefghijklmnopqrstuvwxyz01234",
	}))
	b, _ := json.Marshal(got.Args)
	s := string(b)
	if strings.Contains(s, "plainvalue") {
		t.Errorf("apiKey survived by key: %s", s)
	}
	if strings.Contains(s, "ghp_abcdefghijklmnopqrstuvwxyz01234") {
		t.Errorf("token survived by value: %s", s)
	}
}

// TestDigestNeverCarriesContent — only a fingerprint and a size may leave the
// machine, never the response itself.
func TestDigestNeverCarriesContent(t *testing.T) {
	secretish := `{"stdout":"the quick brown fox jumped over the lazy dog repeatedly"}`
	d := Digest(json.RawMessage(secretish))
	if strings.Contains(d, "quick brown fox") {
		t.Errorf("digest carries content: %q", d)
	}
	if !strings.Contains(d, "bytes=") || !strings.Contains(d, "lines=") {
		t.Errorf("digest = %q, want a hash plus bytes and lines", d)
	}
	if d2 := Digest(json.RawMessage(secretish)); d2 != d {
		t.Error("digest is not deterministic; the server's dedupe depends on it")
	}
}

// TestStatusOf catches the tool that returns successfully while reporting failure
// inside its payload. A hard failure emits no PostToolUse at all, so it is
// detected upstream by the step never closing.
func TestStatusOf(t *testing.T) {
	cases := []struct {
		name string
		body string
		want string
	}{
		{"clean bash", `{"stdout":"ok","stderr":"","interrupted":false}`, StatusOK},
		{"stderr set", `{"stdout":"","stderr":"cat: no such file"}`, StatusError},
		{"interrupted", `{"stdout":"","stderr":"","interrupted":true}`, StatusError},
		{"error field", `{"error":"boom"}`, StatusError},
		{"read result", `{"type":"text","file":{"filePath":"a"}}`, StatusOK},
		{"empty", ``, StatusOK},
		{"unparseable", `not json`, StatusOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := StatusOf(json.RawMessage(tc.body)); got != tc.want {
				t.Errorf("StatusOf(%s) = %q, want %q", tc.body, got, tc.want)
			}
		})
	}
}
