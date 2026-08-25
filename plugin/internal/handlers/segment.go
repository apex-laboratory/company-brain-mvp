package handlers

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"

	"github.com/brainite/plugin/internal/config"
	"github.com/brainite/plugin/internal/hookio"
	"github.com/brainite/plugin/internal/outcome"
	"github.com/brainite/plugin/internal/spool"
	"github.com/brainite/plugin/internal/steps"
)

// Prompt segments the session and injects the matched skill.
//
// Segmentation is keyed on the platform's prompt_id, which is present on
// UserPromptSubmit, PreToolUse, PostToolUse and Stop. That makes a run "all steps
// sharing one prompt_id" — correct under interleaving, stable across re-flushes,
// and it gives externalId an idempotency key the server can dedupe on. The
// earlier scheme of synthesising a segment index from the session id had none of
// those properties.
func Prompt(stdin []byte) error {
	e, cfg, ok := prelude(stdin)
	if !ok {
		return nil
	}

	// Close whatever is still open. Normally Stop already did this; this path
	// covers the turn that ended without one (interrupt, crash, a Stop hook that
	// timed out), so its steps still ship rather than being absorbed into the
	// next task's trajectory.
	records, _ := spool.Read(spool.Path(e.SessionID))
	closeOpenSegments(e.SessionID, records, cfg.TestCommands)

	if err := spool.Append(e.SessionID, spool.Record{
		T:        spool.KindSegmentOpen,
		PromptID: e.PromptID,
		Task:     e.Prompt,
	}); err != nil {
		return err
	}

	// The one deliberate network call on the critical path, under its own hard
	// deadline. Silence on any failure.
	if cfg.Inject.Enabled {
		hookio.InjectContext("UserPromptSubmit", injectFor(cfg, e.Prompt))
	}
	return nil
}

// Stop closes the segment the finished turn belongs to.
//
// last_assistant_message is on the payload, so we never parse transcript_path —
// which also removes the version-drift risk that reading the transcript carried.
func Stop(stdin []byte) error {
	e, cfg, ok := prelude(stdin)
	if !ok {
		return nil
	}
	// stop_hook_active is true when a Stop hook is already running; re-entering
	// would close the segment twice.
	if e.StopHookActive {
		return nil
	}

	records, _ := spool.Read(spool.Path(e.SessionID))

	if msg := e.LastAssistantMessage; msg != "" {
		_ = spool.Append(e.SessionID, spool.Record{
			T:        spool.KindStep,
			PromptID: e.PromptID,
			Index:    spool.NextIndex(records, e.PromptID),
			Type:     steps.TypeAssistantMessage,
			Status:   steps.StatusOK,
			Text:     truncate(msg, 8000),
		})
		records, _ = spool.Read(spool.Path(e.SessionID))
	}

	closeSegment(e.SessionID, records, e.PromptID, cfg.TestCommands)
	spawnFlush()
	return nil
}

// SessionEnd signals the flusher and returns. It does nothing else.
//
// The platform gives *all* SessionEnd hooks from *every* installed plugin 1.5
// seconds combined. A network flush cannot live in that budget, and a plugin that
// eats the shared allowance breaks every other plugin's SessionEnd too.
func SessionEnd(stdin []byte) error {
	e, cfg, ok := prelude(stdin)
	if !ok {
		return nil
	}
	records, _ := spool.Read(spool.Path(e.SessionID))
	closeOpenSegments(e.SessionID, records, cfg.TestCommands)
	spawnFlush()
	return nil
}

// closeOpenSegments closes every segment that has an open record and no close.
func closeOpenSegments(sessionID string, records []spool.Record, testCommands []string) {
	opened := map[string]bool{}
	closed := map[string]bool{}
	var order []string
	for _, r := range records {
		switch r.T {
		case spool.KindSegmentOpen:
			if !opened[r.PromptID] {
				opened[r.PromptID] = true
				order = append(order, r.PromptID)
			}
		case spool.KindSegmentClose:
			closed[r.PromptID] = true
		}
	}
	for _, promptID := range order {
		if !closed[promptID] {
			closeSegment(sessionID, records, promptID, testCommands)
		}
	}
}

func closeSegment(sessionID string, records []spool.Record, promptID string, testCommands []string) {
	if promptID == "" {
		return
	}
	for _, r := range records {
		if r.T == spool.KindSegmentClose && r.PromptID == promptID {
			return // already closed
		}
	}

	// Reconcile open/close pairs so the resolver sees the same statuses the
	// envelope will carry — including the unclosed steps that mean "this tool
	// call failed".
	stepList := reconcile(records, promptID)
	res := outcome.Resolve(stepList, testCommands, consumeMarker(sessionID))

	_ = spool.Append(sessionID, spool.Record{
		T:              spool.KindSegmentClose,
		PromptID:       promptID,
		Outcome:        res.Outcome,
		OutcomeSignals: res.Signals,
	})
}

// reconcile rebuilds a segment's steps in index order with final statuses.
func reconcile(records []spool.Record, promptID string) []spool.Record {
	byIndex := map[int]spool.Record{}
	closedBy := map[string]spool.Record{}
	for _, r := range records {
		if r.PromptID != promptID {
			continue
		}
		switch r.T {
		case spool.KindStepOpen, spool.KindStep:
			byIndex[r.Index] = r
		case spool.KindStepClose:
			closedBy[r.ToolUseID] = r
		}
	}
	out := make([]spool.Record, 0, len(byIndex))
	for i := 0; i < len(byIndex); i++ {
		r, ok := byIndex[i]
		if !ok {
			continue
		}
		if r.T == spool.KindStepOpen {
			if c, done := closedBy[r.ToolUseID]; done {
				r.Status = c.Status
			} else {
				// No PostToolUse ever arrived: the call failed or was denied.
				r.Status = steps.StatusError
			}
		}
		if r.Status == "" {
			r.Status = steps.StatusOK
		}
		out = append(out, r)
	}
	return out
}

// markerPath is how /brain-done reaches the Stop hook. A slash command and a hook
// are separate processes with no shared memory, so a file is the only channel.
func markerPath(sessionID string) string {
	return filepath.Join(config.MarkerDir(), sessionID)
}

// consumeMarker reports whether the user confirmed success, and clears it so the
// confirmation applies to one segment and not to every later turn in the session.
func consumeMarker(sessionID string) bool {
	p := markerPath(sessionID)
	if _, err := os.Stat(p); err != nil {
		return false
	}
	_ = os.Remove(p)
	return true
}

// WriteMarker is called by the /brain-done skill.
func WriteMarker(sessionID string) error {
	if err := os.MkdirAll(config.MarkerDir(), 0o700); err != nil {
		return err
	}
	return os.WriteFile(markerPath(sessionID), []byte("1"), 0o600)
}

// spawnFlush starts a detached flusher and returns immediately. The flush does
// network I/O, so it must not run inside a hook the agent is waiting on.
func spawnFlush() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	cmd := exec.Command(exe, "flush")
	// Own session and process group: the flusher must survive the editor exiting,
	// which is exactly when SessionEnd fires.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = nil, nil, nil
	if err := cmd.Start(); err != nil {
		hookio.Debugf("spawn flush: %v", err)
		return
	}
	_ = cmd.Process.Release()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
