// Package handlers implements one function per hook subcommand.
//
// Everything here runs on the agent's critical path except flush, so the rule is
// absolute: no network I/O, append a line and return. Target <15ms wall clock.
package handlers

import (
	"os"

	"github.com/brainite/plugin/internal/config"
	"github.com/brainite/plugin/internal/hookio"
	"github.com/brainite/plugin/internal/payload"
	"github.com/brainite/plugin/internal/spool"
	"github.com/brainite/plugin/internal/steps"
)

// prelude decodes the event and resolves config, returning ok=false when this
// invocation should do nothing at all — unparseable payload, or a repo that has
// not opted in. Capture is opt-in per repo, so "do nothing" is the common case.
func prelude(stdin []byte) (*payload.Event, *config.Config, bool) {
	e, err := payload.Decode(stdin)
	if err != nil {
		hookio.Debugf("decode: %v", err)
		return nil, nil, false
	}
	cfg, err := config.Load()
	if err != nil {
		hookio.Debugf("config: %v", err)
		return nil, nil, false
	}
	if !cfg.CaptureEnabled(e.Cwd) {
		return e, cfg, false
	}
	return e, cfg, true
}

// SessionStart opens the spool for a new session.
//
// Wired to the startup|clear matcher only. A resumed, forked or post-compact
// session already has an open spool with segments mid-flight; re-opening it would
// double-count them.
func SessionStart(stdin []byte) error {
	e, cfg, ok := prelude(stdin)
	if !ok {
		return nil
	}
	if err := spool.Append(e.SessionID, spool.Record{
		T:          spool.KindSession,
		SessionID:  e.SessionID,
		Cwd:        e.Cwd,
		AgentName:  cfg.AgentName,
		HookSchema: config.HookSchema,
	}); err != nil {
		return err
	}
	// A cached brief, if one is on disk. Never a network call — SessionStart is
	// still the agent's critical path.
	hookio.InjectContext("SessionStart", cachedBrief())
	return nil
}

// PreTool opens a step and assigns its index.
//
// This hook is not optional, and PLUGIN_BUILD.md's hooks.json omitting it is a
// bug: a tool call that *fails* emits PreToolUse and no PostToolUse at all
// (testdata/golden/README.md §1). Without this hook, failures leave no trace and
// the outcome resolver could never see an error. A step opened here and never
// closed is how we know one happened.
//
// Index is assigned here, in arrival order, because parallel tool calls complete
// out of order and the envelope requires contiguous execution order.
func PreTool(stdin []byte) error {
	e, _, ok := prelude(stdin)
	if !ok {
		return nil
	}
	records, _ := spool.Read(spool.Path(e.SessionID))
	m := steps.Map(e)
	return spool.Append(e.SessionID, spool.Record{
		T:         spool.KindStepOpen,
		PromptID:  e.PromptID,
		ToolUseID: e.ToolUseID,
		Index:     spool.NextIndex(records, e.PromptID),
		Type:      m.Type,
		Name:      m.Name,
		Args:      m.Args,
	})
}

// PostTool closes the step its tool_use_id opened.
//
// Never spools tool_response itself — only a digest of it. The response holds
// file contents and shell output from a machine we do not own.
func PostTool(stdin []byte) error {
	e, _, ok := prelude(stdin)
	if !ok {
		return nil
	}
	return spool.Append(e.SessionID, spool.Record{
		T:            spool.KindStepClose,
		PromptID:     e.PromptID,
		ToolUseID:    e.ToolUseID,
		Status:       steps.StatusOf(e.ToolResponse),
		ResultDigest: steps.Digest(e.ToolResponse),
		// The platform hands us duration_ms directly, so latency costs no clock
		// arithmetic between the two hooks.
		LatencyMs: e.DurationMs,
	})
}

// Enable opts the current repo into capture.
func Enable(cwd string) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cwd == "" {
		cwd, err = os.Getwd()
		if err != nil {
			return err
		}
	}
	return cfg.Enable(cwd)
}
