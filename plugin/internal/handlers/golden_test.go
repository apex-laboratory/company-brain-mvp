package handlers_test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// goldenDir holds real hook stdin captured from a live session. See its README
// for the six places the platform disagrees with the specs.
func goldenDir(t *testing.T) string {
	t.Helper()
	wd, _ := os.Getwd()
	dir := filepath.Join(wd, "..", "..", "testdata", "golden")
	if _, err := os.Stat(dir); err != nil {
		t.Skipf("no golden payloads: %v", err)
	}
	return dir
}

func readNDJSON(t *testing.T, path string) []json.RawMessage {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("missing %s", path)
	}
	var out []json.RawMessage
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		out = append(out, json.RawMessage(line))
	}
	return out
}

// run feeds one payload to a subcommand with an isolated BRAINITE_HOME.
func run(t *testing.T, home, sub string, payload []byte) string {
	t.Helper()
	cmd := exec.Command(binary(t), sub)
	cmd.Stdin = strings.NewReader(string(payload))
	cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v (%q)", sub, err, out)
	}
	return string(out)
}

// enable opts the fixtures' cwd into capture, since capture is opt-in per repo
// and every hook is otherwise a deliberate no-op.
func enable(t *testing.T, home, cwd string) {
	t.Helper()
	cmd := exec.Command(binary(t), "enable", cwd)
	cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("enable: %v (%q)", err, out)
	}
}

func cwdOf(t *testing.T, payload []byte) string {
	var m map[string]any
	if err := json.Unmarshal(payload, &m); err != nil {
		t.Fatal(err)
	}
	s, _ := m["cwd"].(string)
	return s
}

// TestGoldenReplay drives the whole captured session through the binary and
// asserts the spool that comes out.
//
// Payload shapes drift between platform releases. Replaying the fixtures makes
// drift a failing test rather than a session that silently captures nothing.
func TestGoldenReplay(t *testing.T) {
	dir := goldenDir(t)
	home := t.TempDir()

	sessionStart := readNDJSON(t, filepath.Join(dir, "SessionStart.ndjson"))
	prompts := readNDJSON(t, filepath.Join(dir, "UserPromptSubmit.ndjson"))
	pre := readNDJSON(t, filepath.Join(dir, "PreToolUse.ndjson"))
	post := readNDJSON(t, filepath.Join(dir, "PostToolUse.ndjson"))
	stops := readNDJSON(t, filepath.Join(dir, "Stop.ndjson"))

	enable(t, home, cwdOf(t, sessionStart[0]))

	run(t, home, "session-start", sessionStart[0])
	run(t, home, "prompt", prompts[0])
	// Replay the first turn's tool calls in the order the platform delivered them.
	firstPrompt := fieldOf(t, prompts[0], "prompt_id")
	for _, p := range pre {
		if fieldOf(t, p, "prompt_id") == firstPrompt {
			run(t, home, "pretool", p)
		}
	}
	for _, p := range post {
		if fieldOf(t, p, "prompt_id") == firstPrompt {
			run(t, home, "posttool", p)
		}
	}
	run(t, home, "stop", stops[0])

	spoolFiles, err := filepath.Glob(filepath.Join(home, "spool", "*.ndjson"))
	if err != nil || len(spoolFiles) != 1 {
		t.Fatalf("expected one spool file, got %v (%v)", spoolFiles, err)
	}
	records := decodeSpool(t, spoolFiles[0])

	counts := map[string]int{}
	for _, r := range records {
		counts[r["t"].(string)]++
	}
	if counts["session"] != 1 {
		t.Errorf("session records = %d, want 1", counts["session"])
	}
	if counts["segment_open"] != 1 {
		t.Errorf("segment_open = %d, want 1", counts["segment_open"])
	}
	if counts["segment_close"] != 1 {
		t.Errorf("segment_close = %d, want 1", counts["segment_close"])
	}
	if counts["step_open"] == 0 {
		t.Fatal("no steps captured — the tool payload shape has drifted")
	}

	// The captured session read a file, ran a shell command, grepped and wrote a
	// file. If the mapping table drifts, these disappear.
	types := map[string]bool{}
	for _, r := range records {
		if r["t"] == "step_open" {
			types[r["type"].(string)] = true
		}
	}
	for _, want := range []string{"file_read", "file_write", "shell", "tool_call"} {
		if !types[want] {
			t.Errorf("no %s step in replayed session; mapping drifted (saw %v)", want, types)
		}
	}
}

// TestGoldenNeverStoresToolResponse is the leak assertion at the spool boundary.
//
// tool_response carries file contents and shell output from a machine we do not
// own. Only its digest may ever be written. This asserts against the real
// captured responses, not a synthetic one.
func TestGoldenNeverStoresToolResponse(t *testing.T) {
	dir := goldenDir(t)
	home := t.TempDir()
	post := readNDJSON(t, filepath.Join(dir, "PostToolUse.ndjson"))
	enable(t, home, cwdOf(t, post[0]))

	for _, p := range post {
		run(t, home, "pretool", p)
		run(t, home, "posttool", p)
	}

	files, _ := filepath.Glob(filepath.Join(home, "spool", "*.ndjson"))
	if len(files) == 0 {
		t.Fatal("nothing spooled")
	}
	raw, err := os.ReadFile(files[0])
	if err != nil {
		t.Fatal(err)
	}
	spooled := string(raw)

	for _, p := range post {
		var m map[string]json.RawMessage
		if err := json.Unmarshal(p, &m); err != nil {
			continue
		}
		resp, ok := m["tool_response"]
		if !ok {
			continue
		}
		var decoded map[string]any
		if err := json.Unmarshal(resp, &decoded); err != nil {
			continue
		}
		// Any sufficiently long string from the response must not appear verbatim.
		for key, v := range decoded {
			s, ok := v.(string)
			if !ok || len(s) < 40 {
				continue
			}
			if strings.Contains(spooled, s) {
				t.Errorf("tool_response.%s leaked verbatim into the spool", key)
			}
		}
	}
}

func fieldOf(t *testing.T, raw json.RawMessage, key string) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return ""
	}
	s, _ := m[key].(string)
	return s
}

func decodeSpool(t *testing.T, path string) []map[string]any {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("unparseable spool line: %v", err)
		}
		out = append(out, m)
	}
	return out
}
