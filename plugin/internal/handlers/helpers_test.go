package handlers_test

import (
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
)

func stringReader(s string) io.Reader { return strings.NewReader(s) }

// runEnv is run() with extra environment, e.g. an API base pointing at a stub.
func runEnv(t *testing.T, home, sub string, payload []byte, extra []string) string {
	t.Helper()
	cmd := exec.Command(binary(t), sub)
	cmd.Stdin = strings.NewReader(string(payload))
	cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
	cmd.Env = append(cmd.Env, extra...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v (%q)", sub, err, out)
	}
	return string(out)
}

// doneIn runs `brainite-hook done` from a given working directory, the way the
// /brain-done skill does.
func doneIn(t *testing.T, home, cwd string, extra []string) string {
	t.Helper()
	cmd := exec.Command(binary(t), "done", cwd)
	cmd.Env = append(os.Environ(), "BRAINITE_HOME="+home)
	cmd.Env = append(cmd.Env, extra...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("done: %v (%q)", err, out)
	}
	return string(out)
}
