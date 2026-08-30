// Package hookio is the safety wrapper around every hook invocation.
//
// One rule dominates this package: **the process always exits 0.** On
// UserPromptSubmit, exit code 2 is "blocking" and erases the user's typed prompt.
// A panic in our capture code must never delete somebody's work in their editor,
// so Run recovers everything and exits 0 regardless.
//
// Second rule: nothing on stderr on the happy path. Several events surface stderr
// to the model, and polluting the context is the opposite of this plugin's job.
package hookio

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// Handler does the work for one subcommand. Its error is logged to the debug log
// and otherwise swallowed — the exit code never reflects it.
type Handler func(stdin []byte) error

// Run executes h with a hard guarantee of exit 0.
func Run(name string, h Handler) {
	defer func() {
		if r := recover(); r != nil {
			Debugf("panic in %s: %v", name, r)
		}
		os.Exit(0)
	}()

	stdin, err := readStdin()
	if err != nil {
		Debugf("%s: read stdin: %v", name, err)
		os.Exit(0)
	}
	if err := h(stdin); err != nil {
		Debugf("%s: %v", name, err)
	}
	os.Exit(0)
}

// readStdin drains stdin under a deadline so a harness that opens the pipe and
// never writes cannot hang a hook on the agent's critical path.
func readStdin() ([]byte, error) {
	type result struct {
		b   []byte
		err error
	}
	ch := make(chan result, 1)
	go func() {
		b, err := io.ReadAll(os.Stdin)
		ch <- result{b, err}
	}()
	select {
	case r := <-ch:
		return r.b, r.err
	case <-time.After(2 * time.Second):
		return nil, fmt.Errorf("stdin read timed out")
	}
}

// InjectContext prints the additionalContext form for the given event. This is
// the only thing we ever write to stdout; when there is nothing to inject we
// print nothing at all, because an empty or error string in the agent's context
// is worse than silence.
func InjectContext(eventName, context string) {
	if context == "" {
		return
	}
	out := map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     eventName,
			"additionalContext": context,
		},
	}
	b, err := json.Marshal(out)
	if err != nil {
		return
	}
	fmt.Fprintln(os.Stdout, string(b))
}

// Debugf writes to ~/.brainite/debug.log when BRAINITE_DEBUG is set. Never stderr.
func Debugf(format string, args ...any) {
	if os.Getenv("BRAINITE_DEBUG") == "" {
		return
	}
	path := os.Getenv("BRAINITE_DEBUG_LOG")
	if path == "" {
		dir := os.Getenv("BRAINITE_HOME")
		if dir == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return
			}
			dir = filepath.Join(home, ".brainite")
		}
		path = filepath.Join(dir, "debug.log")
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s "+format+"\n", append([]any{time.Now().Format(time.RFC3339)}, args...)...)
}
