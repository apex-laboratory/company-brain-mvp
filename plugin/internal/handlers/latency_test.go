package handlers_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// Hot-path budgets. posttool runs on every tool call and prompt on every prompt;
// a slow hook is a tax on every interaction in the session, which is the whole
// reason this is a compiled binary rather than a script.
//
// The budget is measured end-to-end including process spawn, because that is
// what the user actually waits for.
const (
	postToolBudget = 15 * time.Millisecond
	promptBudget   = 800 * time.Millisecond
)

func TestLatencyPostTool(t *testing.T) {
	if testing.Short() {
		t.Skip("latency test skipped in -short")
	}
	dir := goldenDir(t)
	home := t.TempDir()
	post := readNDJSON(t, filepath.Join(dir, "PostToolUse.ndjson"))
	enable(t, home, cwdOf(t, post[0]))

	p95 := measure(t, home, "posttool", post[0], 40)
	t.Logf("posttool p95 = %v (budget %v)", p95, postToolBudget)
	if p95 > postToolBudget {
		t.Errorf("posttool p95 %v exceeds %v", p95, postToolBudget)
	}
}

// TestLatencyPromptDeadline asserts the injection deadline is actually enforced.
//
// Pointed at a server that never answers — the failure mode that matters, since
// a hook which waits on a wedged endpoint stalls every prompt the developer
// types. An unroutable IP would not prove this: the stack refuses those fast.
func TestLatencyPromptDeadline(t *testing.T) {
	if testing.Short() {
		t.Skip("latency test skipped in -short")
	}
	blocked := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-blocked // hang until the test ends
	}))
	// Order matters: Close waits for in-flight handlers, so the channel must be
	// closed *before* it runs. Deferred calls are LIFO, so this pair is the right
	// way round and swapping it deadlocks the test.
	defer srv.Close()
	defer close(blocked)

	home := t.TempDir()
	enable(t, home, "/tmp")
	if err := os.WriteFile(filepath.Join(home, "credentials"), []byte("bk_test"), 0o600); err != nil {
		t.Fatal(err)
	}

	payload, _ := json.Marshal(map[string]any{
		"session_id": "s-deadline", "cwd": "/tmp", "prompt_id": "p1",
		"prompt": "a prompt long enough to be worth an injection lookup",
	})
	extra := []string{"BRAINITE_API_BASE=" + srv.URL + "/api/v1"}
	p95 := measureEnv(t, home, "prompt", payload, 5, extra)
	t.Logf("prompt p95 against a hung server = %v (deadline %v)", p95, promptBudget)
	// The deadline plus process spawn. If this is anywhere near the platform's
	// 30s timeout, the context deadline is not wired through.
	if p95 > 2*promptBudget {
		t.Fatalf("prompt p95 %v exceeds %v — the injection deadline is not being honoured", p95, 2*promptBudget)
	}
	// And it must actually have waited: a run that returns instantly is not
	// exercising the network path at all, so the test would prove nothing.
	if p95 < 300*time.Millisecond {
		t.Fatalf("prompt returned in %v against a hung server — injection never ran, so the deadline is untested", p95)
	}
}

func measure(t *testing.T, home, sub string, payload []byte, n int) time.Duration {
	return measureEnv(t, home, sub, payload, n, nil)
}

func measureEnv(t *testing.T, home, sub string, payload []byte, n int, extra []string) time.Duration {
	t.Helper()
	bin := binary(t)
	var samples []time.Duration
	for i := 0; i < n; i++ {
		// Vary tool_use_id so each iteration writes a distinct step rather than
		// measuring a no-op.
		body := strings.Replace(string(payload), `"tool_use_id":"`, fmt.Sprintf(`"tool_use_id":"i%d`, i), 1)
		cmd := exec.Command(bin, sub)
		cmd.Stdin = strings.NewReader(body)
		cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
		cmd.Env = append(cmd.Env, extra...)
		start := time.Now()
		if err := cmd.Run(); err != nil {
			t.Fatalf("%s: %v", sub, err)
		}
		samples = append(samples, time.Since(start))
	}
	sort.Slice(samples, func(i, j int) bool { return samples[i] < samples[j] })
	return samples[int(float64(len(samples))*0.95)-0]
}
