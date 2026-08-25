package handlers_test

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// captured is one POST /runs body the stub received.
type captured struct {
	Path string
	Key  string
	Body map[string]any
}

type stub struct {
	mu   sync.Mutex
	got  []captured
	seen map[string]bool
	srv  *httptest.Server
}

// newStub stands in for the real API so the flush path can be asserted without
// the stack. It mirrors the one behaviour the client depends on: the UPSERT on
// externalId, reported back as duplicate:true.
func newStub(t *testing.T) *stub {
	t.Helper()
	s := &stub{seen: map[string]bool{}}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)

		s.mu.Lock()
		s.got = append(s.got, captured{Path: r.URL.Path, Key: r.Header.Get("X-API-Key"), Body: body})
		extID, _ := body["externalId"].(string)
		dup := s.seen[extID]
		s.seen[extID] = true
		s.mu.Unlock()

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": map[string]any{"runId": "run_123", "duplicate": dup},
		})
	}))
	t.Cleanup(s.srv.Close)
	return s
}

func (s *stub) calls() []captured {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]captured, len(s.got))
	copy(out, s.got)
	return out
}

// setupSession replays a full turn into an isolated home and returns it.
func setupSession(t *testing.T, home string, extra ...string) {
	t.Helper()
	dir := goldenDir(t)
	sessionStart := readNDJSON(t, filepath.Join(dir, "SessionStart.ndjson"))
	prompts := readNDJSON(t, filepath.Join(dir, "UserPromptSubmit.ndjson"))
	pre := readNDJSON(t, filepath.Join(dir, "PreToolUse.ndjson"))
	post := readNDJSON(t, filepath.Join(dir, "PostToolUse.ndjson"))
	stops := readNDJSON(t, filepath.Join(dir, "Stop.ndjson"))

	enable(t, home, cwdOf(t, sessionStart[0]))
	runEnv(t, home, "session-start", sessionStart[0], extra)
	runEnv(t, home, "prompt", prompts[0], extra)
	first := fieldOf(t, prompts[0], "prompt_id")
	for _, p := range pre {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home, "pretool", p, extra)
		}
	}
	for _, p := range post {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home, "posttool", p, extra)
		}
	}
	runEnv(t, home, "stop", stops[0], extra)
}

// TestFlushRoundTrip drives a captured session all the way to a POST body and
// asserts the envelope the server actually requires.
func TestFlushRoundTrip(t *testing.T) {
	s := newStub(t)
	home := t.TempDir()
	env := []string{"BRAINITE_API_BASE=" + s.srv.URL + "/api/v1"}

	setupSession(t, home, env...)
	if err := os.WriteFile(filepath.Join(home, "credentials"), []byte("bk_roundtrip"), 0o600); err != nil {
		t.Fatal(err)
	}
	runEnv(t, home, "flush", []byte("{}"), env)

	calls := s.calls()
	if len(calls) != 1 {
		t.Fatalf("expected one POST, got %d", len(calls))
	}
	c := calls[0]
	if c.Path != "/api/v1/runs" {
		t.Errorf("path = %q, want /api/v1/runs", c.Path)
	}
	if c.Key != "bk_roundtrip" {
		t.Errorf("X-API-Key = %q; the credential must be sent as a header", c.Key)
	}

	// Envelope shape, field by field, against RunIngestRequest.
	if got := c.Body["harness"]; got != "claude_code" {
		t.Errorf("harness = %v, want claude_code", got)
	}
	if got, _ := c.Body["agentName"].(string); got == "" {
		t.Error("agentName is required and was empty")
	}
	if got, _ := c.Body["task"].(string); got == "" {
		t.Error("task is required and was empty")
	}
	extID, _ := c.Body["externalId"].(string)
	if !strings.HasPrefix(extID, "cc_") {
		t.Errorf("externalId = %q, want a cc_<session>_<prompt> key", extID)
	}
	outcome, _ := c.Body["outcome"].(string)
	switch outcome {
	case "success", "failure", "partial", "ambiguous", "unknown":
	default:
		t.Errorf("outcome = %q is not a value the server accepts", outcome)
	}

	steps, _ := c.Body["steps"].([]any)
	if len(steps) == 0 {
		t.Fatal("no steps in the envelope")
	}
	// The server rejects any list not indexed contiguously from 0 in execution
	// order, so this is the assertion that keeps a real push from 422-ing.
	for i, raw := range steps {
		st := raw.(map[string]any)
		if int(st["index"].(float64)) != i {
			t.Fatalf("step %d has index %v — the server would reject this run", i, st["index"])
		}
		typ, _ := st["type"].(string)
		switch typ {
		case "tool_call", "file_read", "file_write", "shell", "assistant_message":
		default:
			t.Errorf("step %d type %q is not in the envelope enum", i, typ)
		}
	}
}

// TestFlushIsIdempotent — a re-flush must not create a second run. The spool is
// marked sent only after a 2xx, so a crash mid-flush re-sends, and externalId is
// what makes that safe.
func TestFlushIsIdempotent(t *testing.T) {
	s := newStub(t)
	home := t.TempDir()
	env := []string{"BRAINITE_API_BASE=" + s.srv.URL + "/api/v1"}

	setupSession(t, home, env...)
	if err := os.WriteFile(filepath.Join(home, "credentials"), []byte("bk_x"), 0o600); err != nil {
		t.Fatal(err)
	}
	runEnv(t, home, "flush", []byte("{}"), env)
	runEnv(t, home, "flush", []byte("{}"), env)
	runEnv(t, home, "flush", []byte("{}"), env)

	if n := len(s.calls()); n != 1 {
		t.Fatalf("three flushes produced %d POSTs, want 1 — the .sent sidecar is not holding", n)
	}
}

// TestFlushWithoutCredentialKeepsSpool — no key means nothing is uploaded and
// nothing is lost. The spool drains once a credential exists.
func TestFlushWithoutCredentialKeepsSpool(t *testing.T) {
	s := newStub(t)
	home := t.TempDir()
	env := []string{"BRAINITE_API_BASE=" + s.srv.URL + "/api/v1"}

	setupSession(t, home, env...)
	runEnv(t, home, "flush", []byte("{}"), env)

	if n := len(s.calls()); n != 0 {
		t.Fatalf("posted %d runs without a credential, want 0", n)
	}
	files, _ := filepath.Glob(filepath.Join(home, "spool", "*.ndjson"))
	if len(files) != 1 {
		t.Fatalf("spool was not preserved: %v", files)
	}
}

// TestDoneMarksHumanConfirmed — the whole point of /brain-done: one confirmed run
// is distillable where three inferred ones would be needed.
func TestDoneMarksHumanConfirmed(t *testing.T) {
	s := newStub(t)
	home := t.TempDir()
	env := []string{"BRAINITE_API_BASE=" + s.srv.URL + "/api/v1"}
	dir := goldenDir(t)

	sessionStart := readNDJSON(t, filepath.Join(dir, "SessionStart.ndjson"))
	prompts := readNDJSON(t, filepath.Join(dir, "UserPromptSubmit.ndjson"))
	pre := readNDJSON(t, filepath.Join(dir, "PreToolUse.ndjson"))
	post := readNDJSON(t, filepath.Join(dir, "PostToolUse.ndjson"))
	stops := readNDJSON(t, filepath.Join(dir, "Stop.ndjson"))
	cwd := cwdOf(t, sessionStart[0])

	enable(t, home, cwd)
	runEnv(t, home, "session-start", sessionStart[0], env)
	runEnv(t, home, "prompt", prompts[0], env)
	first := fieldOf(t, prompts[0], "prompt_id")
	for _, p := range pre {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home, "pretool", p, env)
		}
	}
	for _, p := range post {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home, "posttool", p, env)
		}
	}

	// The user confirms, from the repo directory, before the turn ends.
	doneIn(t, home, cwd, env)
	runEnv(t, home, "stop", stops[0], env)

	if err := os.WriteFile(filepath.Join(home, "credentials"), []byte("bk_done"), 0o600); err != nil {
		t.Fatal(err)
	}
	runEnv(t, home, "flush", []byte("{}"), env)

	calls := s.calls()
	if len(calls) != 1 {
		t.Fatalf("expected one POST, got %d", len(calls))
	}
	body := calls[0].Body
	if body["outcome"] != "success" {
		t.Errorf("outcome = %v, want success after /brain-done", body["outcome"])
	}
	signals, _ := body["outcomeSignals"].(map[string]any)
	if signals["humanConfirmed"] != true {
		t.Errorf("humanConfirmed = %v, want true (signals: %v)", signals["humanConfirmed"], signals)
	}
	if signals["inferred"] == true {
		t.Error("a human-confirmed run must not also be marked inferred")
	}
}

// TestMarkerIsConsumedOnce — a confirmation applies to the segment it was given
// for, not to every later turn in the session.
func TestMarkerIsConsumedOnce(t *testing.T) {
	home := t.TempDir()
	dir := goldenDir(t)
	sessionStart := readNDJSON(t, filepath.Join(dir, "SessionStart.ndjson"))
	cwd := cwdOf(t, sessionStart[0])
	enable(t, home, cwd)
	runEnv(t, home, "session-start", sessionStart[0], nil)
	doneIn(t, home, cwd, nil)

	markers, _ := filepath.Glob(filepath.Join(home, "marker", "*"))
	if len(markers) != 1 {
		t.Fatalf("expected one marker, got %v", markers)
	}
	stops := readNDJSON(t, filepath.Join(dir, "Stop.ndjson"))
	runEnv(t, home, "stop", stops[0], nil)

	markers, _ = filepath.Glob(filepath.Join(home, "marker", "*"))
	if len(markers) != 0 {
		t.Errorf("marker survived the segment close: %v — a later turn would inherit the confirmation", markers)
	}
}
