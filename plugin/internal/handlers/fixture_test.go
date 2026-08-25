package handlers_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestWriteEnvelopeFixture regenerates the envelope the backend test suite
// validates against the real pydantic schema.
//
// The two halves of this contract live in different languages, so nothing but a
// shared fixture stops them drifting: the Go side can keep emitting a field the
// server silently rejects (CamelRequestModel forbids unknown keys) and no test on
// either side would notice. Run with BRAINITE_WRITE_FIXTURE=1 after any change to
// the envelope, and commit the result.
func TestWriteEnvelopeFixture(t *testing.T) {
	if os.Getenv("BRAINITE_WRITE_FIXTURE") == "" {
		t.Skip("set BRAINITE_WRITE_FIXTURE=1 to regenerate testdata/envelope")
	}
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

	// Case 1: an ordinary inferred run.
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
	runEnv(t, home, "stop", stops[0], env)

	// Case 2: the same session, confirmed by a human — the shape that bypasses
	// the cluster-size wait, and the one most important to get right.
	home2 := t.TempDir()
	enable(t, home2, cwd)
	runEnv(t, home2, "session-start", sessionStart[0], env)
	runEnv(t, home2, "prompt", prompts[0], env)
	for _, p := range pre {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home2, "pretool", p, env)
		}
	}
	for _, p := range post {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home2, "posttool", p, env)
		}
	}
	doneIn(t, home2, cwd, env)
	runEnv(t, home2, "stop", stops[0], env)

	// Case 3: a failed tool call — a step opened and never closed.
	home3 := t.TempDir()
	enable(t, home3, cwd)
	runEnv(t, home3, "session-start", sessionStart[0], env)
	runEnv(t, home3, "prompt", prompts[0], env)
	for _, p := range pre {
		if fieldOf(t, p, "prompt_id") == first {
			runEnv(t, home3, "pretool", p, env)
		}
	}
	// Close all but the last: the unclosed one becomes status:error.
	for i, p := range post {
		if fieldOf(t, p, "prompt_id") == first && i < len(post)-1 {
			runEnv(t, home3, "posttool", p, env)
		}
	}
	runEnv(t, home3, "stop", stops[0], env)

	for _, h := range []string{home, home2, home3} {
		if err := os.WriteFile(filepath.Join(h, "credentials"), []byte("bk_fixture"), 0o600); err != nil {
			t.Fatal(err)
		}
		runEnv(t, h, "flush", []byte("{}"), env)
	}

	calls := s.calls()
	if len(calls) != 3 {
		t.Fatalf("expected 3 envelopes, got %d", len(calls))
	}

	wd, _ := os.Getwd()
	outDir := filepath.Join(wd, "..", "..", "testdata", "envelope")
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Fatal(err)
	}
	names := []string{"inferred_run.json", "human_confirmed_run.json", "failed_step_run.json"}
	for i, name := range names {
		b, err := json.MarshalIndent(calls[i].Body, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(outDir, name), append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote testdata/envelope/%s", name)
	}
}
