// Package envelope turns spooled records into the Feature 29 run envelope.
//
// This is where the two shapes meet: the spool is an append-only stream of
// half-open steps keyed by tool_use_id, and POST /runs wants a closed list of
// steps indexed contiguously from 0 in execution order. Reconciliation happens
// here, and it is the only place that knows a step opened and never closed means
// the tool *failed* (testdata/golden/README.md §1).
package envelope

import (
	"github.com/brainite/plugin/internal/spool"
	"github.com/brainite/plugin/internal/steps"
)

// Step mirrors app/modules/runs/schemas.py::RunStep. Fields are camelCase because
// the API's request models are CamelRequestModel, and they reject unknown keys —
// a misspelled field here is a 422, not a silent drop.
type Step struct {
	Index        int            `json:"index"`
	Type         string         `json:"type"`
	Name         string         `json:"name,omitempty"`
	Args         map[string]any `json:"args,omitempty"`
	Text         string         `json:"text,omitempty"`
	ResultDigest string         `json:"resultDigest,omitempty"`
	Status       string         `json:"status"`
	LatencyMs    int            `json:"latencyMs,omitempty"`
}

// Run mirrors RunIngestRequest.
type Run struct {
	ExternalID     string          `json:"externalId,omitempty"`
	AgentName      string          `json:"agentName"`
	Task           string          `json:"task"`
	Outcome        string          `json:"outcome"`
	OutcomeSignals map[string]bool `json:"outcomeSignals,omitempty"`
	Harness        string          `json:"harness"`
	StartedAt      string          `json:"startedAt,omitempty"`
	EndedAt        string          `json:"endedAt,omitempty"`
	DurationMs     int             `json:"durationMs,omitempty"`
	Steps          []Step          `json:"steps"`

	// PromptID is the spool's segment key, carried for the .sent sidecar. Not
	// serialized — the server's idempotency key is ExternalID.
	PromptID string `json:"-"`
}

// Segment is one closed run segment reassembled from the spool.
type Segment struct {
	PromptID string
	Task     string
	Steps    []Step
	Outcome  string
	Signals  map[string]bool
	OpenedAt string
	ClosedAt string
}

// Assemble reconciles a whole spool file into its closed segments.
//
// Steps are ordered by the index PreToolUse assigned, not by close order:
// parallel tool calls complete out of order, and a shuffled list would distil
// into a procedure whose steps are in the wrong order — worse than no procedure.
func Assemble(records []spool.Record) (sessionID, cwd, agentName string, segments []Segment) {
	type openStep struct {
		rec    spool.Record
		closed bool
	}

	bySegment := map[string]map[int]*openStep{}
	order := []string{}
	meta := map[string]*Segment{}
	byToolUse := map[string]*openStep{}

	for _, r := range records {
		switch r.T {
		case spool.KindSession:
			sessionID, cwd, agentName = r.SessionID, r.Cwd, r.AgentName

		case spool.KindSegmentOpen:
			if _, seen := meta[r.PromptID]; !seen {
				order = append(order, r.PromptID)
				meta[r.PromptID] = &Segment{PromptID: r.PromptID, Task: r.Task, OpenedAt: r.At}
				bySegment[r.PromptID] = map[int]*openStep{}
			}

		case spool.KindStepOpen:
			if bySegment[r.PromptID] == nil {
				// A step whose segment_open we never saw (spool truncated, or the
				// session started before capture was enabled). Keep it: an
				// orphaned segment with a blank task still carries a trajectory,
				// and the server will judge it.
				order = appendUnique(order, r.PromptID)
				meta[r.PromptID] = &Segment{PromptID: r.PromptID}
				bySegment[r.PromptID] = map[int]*openStep{}
			}
			st := &openStep{rec: r}
			bySegment[r.PromptID][r.Index] = st
			if r.ToolUseID != "" {
				byToolUse[r.ToolUseID] = st
			}

		case spool.KindStepClose:
			st, ok := byToolUse[r.ToolUseID]
			if !ok {
				continue
			}
			st.closed = true
			st.rec.Status = r.Status
			st.rec.ResultDigest = r.ResultDigest
			st.rec.LatencyMs = r.LatencyMs

		case spool.KindStep:
			// A self-contained step with no open/close pair (the assistant
			// message written at Stop).
			if bySegment[r.PromptID] == nil {
				order = appendUnique(order, r.PromptID)
				meta[r.PromptID] = &Segment{PromptID: r.PromptID}
				bySegment[r.PromptID] = map[int]*openStep{}
			}
			bySegment[r.PromptID][r.Index] = &openStep{rec: r, closed: true}

		case spool.KindSegmentClose:
			if m, ok := meta[r.PromptID]; ok {
				m.Outcome, m.Signals, m.ClosedAt = r.Outcome, r.OutcomeSignals, r.At
			}
		}
	}

	for _, promptID := range order {
		m := meta[promptID]
		if m == nil || m.Outcome == "" {
			// Still open: the segment has not been closed by a prompt or a Stop.
			// Leaving it in the spool is deliberate — the next flush picks it up
			// once it closes, rather than shipping half a run now.
			continue
		}
		stepMap := bySegment[promptID]
		out := make([]Step, 0, len(stepMap))
		// Walk indices densely so the emitted list is contiguous from 0 even if
		// the spool has a hole (a hook that never got to write).
		for i := 0; i < len(stepMap); i++ {
			st, ok := stepMap[i]
			if !ok {
				continue
			}
			rec := st.rec
			status := rec.Status
			if status == "" {
				if st.closed {
					status = steps.StatusOK
				} else {
					// Opened, never closed. The tool call failed, was denied, or
					// the session died. This is the *only* way an error reaches
					// the trace, since a failed tool emits no PostToolUse.
					status = steps.StatusError
				}
			}
			out = append(out, Step{
				Index:        len(out),
				Type:         rec.Type,
				Name:         rec.Name,
				Args:         rec.Args,
				Text:         rec.Text,
				ResultDigest: rec.ResultDigest,
				Status:       status,
				LatencyMs:    rec.LatencyMs,
			})
		}
		m.Steps = out
		segments = append(segments, *m)
	}
	return sessionID, cwd, agentName, segments
}

func appendUnique(xs []string, s string) []string {
	for _, x := range xs {
		if x == s {
			return xs
		}
	}
	return append(xs, s)
}
