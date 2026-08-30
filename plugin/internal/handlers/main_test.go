package handlers_test

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

var builtBinary string

// TestMain builds the binary the tests drive.
//
// These tests shell out, because the behaviour under test *is* the process exit
// code and its wall-clock cost. Building here rather than relying on `make build`
// removes a real trap: `go test` against a stale bin/ silently exercises the last
// build, and a latency or deadline assertion would then pass or fail for reasons
// that have nothing to do with the code being tested.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "brainite-bin")
	if err != nil {
		fmt.Fprintln(os.Stderr, "tempdir:", err)
		os.Exit(1)
	}
	defer os.RemoveAll(dir)

	builtBinary = filepath.Join(dir, "brainite-hook")
	cmd := exec.Command("go", "build", "-o", builtBinary, "../../cmd/brainite-hook")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "build failed: %v\n%s", err, out)
		os.Exit(1)
	}

	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}
