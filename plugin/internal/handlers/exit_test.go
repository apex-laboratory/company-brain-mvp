package handlers_test

import (
	"os"
	"os/exec"
	"testing"
)

// binary is built once by `make test`; the tests shell out to it because the
// thing under test *is* the process exit code, which cannot be observed from
// inside the same process.
func binary(t *testing.T) string {
	t.Helper()
	if builtBinary == "" {
		t.Fatal("TestMain did not build the binary")
	}
	return builtBinary
}

var subcommands = []string{
	"session-start", "prompt", "pretool", "posttool", "stop", "flush", "session-end",
}

// TestAlwaysExitsZero is the single highest-risk behaviour in this codebase.
//
// On UserPromptSubmit, exit code 2 is "blocking" and *erases the user's prompt*.
// A capture plugin that deletes what someone typed because a spool file was
// unwritable is a data-loss bug in their editor, and it would be ours. Every
// input below is one that a naive implementation would exit non-zero on.
func TestAlwaysExitsZero(t *testing.T) {
	bin := binary(t)

	cases := []struct {
		name  string
		stdin string
		env   []string
	}{
		{"empty stdin", "", nil},
		{"not json", "this is not json at all", nil},
		{"truncated json", `{"session_id": "abc"`, nil},
		{"json array", `[1,2,3]`, nil},
		{"json null", `null`, nil},
		{"no session_id", `{"hook_event_name":"Stop"}`, nil},
		{"wrong types", `{"session_id":123,"cwd":[],"prompt_id":{}}`, nil},
		{"huge string", `{"session_id":"` + string(make([]byte, 0)) + repeat("a", 200000) + `"}`, nil},
		{"nested bomb", `{"session_id":"s","cwd":"/tmp","tool_input":` + nest(60) + `}`, nil},
		{"unwritable spool", `{"session_id":"s","cwd":"/tmp","prompt_id":"p","prompt":"a real prompt long enough to inject"}`,
			[]string{"BRAINITE_HOME=/dev/null/nope"}},
		{"unreachable api", `{"session_id":"s","cwd":"/tmp","prompt_id":"p","prompt":"a real prompt long enough to inject"}`,
			[]string{"BRAINITE_API_KEY=bk_test", "BRAINITE_API_BASE=http://127.0.0.1:1/api/v1"}},
	}

	for _, sub := range subcommands {
		for _, tc := range cases {
			t.Run(sub+"/"+tc.name, func(t *testing.T) {
				home := t.TempDir()
				cmd := exec.Command(bin, sub)
				cmd.Stdin = stringReader(tc.stdin)
				cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
				cmd.Env = append(cmd.Env, tc.env...)
				out, err := cmd.CombinedOutput()
				if err != nil {
					t.Fatalf("exit non-zero for %s/%s: %v (output: %q)", sub, tc.name, err, out)
				}
			})
		}
	}
}

// TestUnknownSubcommandExitsZero — a stale hooks.json pointing at a subcommand a
// newer binary removed must degrade to capturing nothing, not to a hook error on
// every prompt.
func TestUnknownSubcommandExitsZero(t *testing.T) {
	cmd := exec.Command(binary(t), "no-such-subcommand")
	cmd.Env = append(os.Environ(), "BRAINITE_HOME="+t.TempDir())
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("expected exit 0, got %v (%q)", err, out)
	}
}

// TestSilentOnHappyPath — several events surface stderr to the model. Anything we
// print there pollutes the very context this plugin exists to improve.
func TestSilentOnHappyPath(t *testing.T) {
	bin := binary(t)
	home := t.TempDir()
	for _, sub := range subcommands {
		cmd := exec.Command(bin, sub)
		cmd.Stdin = stringReader(`{"session_id":"s1","cwd":"/tmp","prompt_id":"p1"}`)
		cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
		var stderr writeCollector
		cmd.Stderr = &stderr
		if err := cmd.Run(); err != nil {
			t.Fatalf("%s: %v", sub, err)
		}
		if len(stderr.b) > 0 {
			t.Errorf("%s wrote to stderr: %q", sub, stderr.b)
		}
	}
}

type writeCollector struct{ b []byte }

func (w *writeCollector) Write(p []byte) (int, error) { w.b = append(w.b, p...); return len(p), nil }

func repeat(s string, n int) string {
	out := make([]byte, 0, len(s)*n)
	for i := 0; i < n; i++ {
		out = append(out, s...)
	}
	return string(out)
}

func nest(depth int) string {
	s := `"x"`
	for i := 0; i < depth; i++ {
		s = `{"a":` + s + `}`
	}
	return s
}
