package envelope

import (
	"encoding/json"
	"fmt"
)

// Server-side limits, mirrored client-side. Mirroring rather than discovering
// them: blowing the cap costs a 413 or a 422 *after* the trace has crossed the
// wire, and the spool entry is gone the moment we get a 4xx.
const (
	MaxSteps     = 400       // settings.run_max_steps
	MaxBodyBytes = 1 << 20   // settings.run_max_body_bytes (1 MB)
	bodyHeadroom = 32 * 1024 // room for task/signals/framing around the steps
)

// Build turns closed segments into pushable runs.
//
// Trivial segments are dropped here rather than sent to be rejected: a segment
// with fewer than minSteps steps and no tool call is conversation, not a run, and
// spending a request to learn that is pure waste on both ends.
func Build(sessionID, agentName string, segments []Segment, minSteps int) []Run {
	var runs []Run
	for _, seg := range segments {
		if isTrivial(seg, minSteps) {
			continue
		}
		task := seg.Task
		if task == "" {
			// A segment whose segment_open we never saw. The task is the one
			// field the server cannot default, so name it honestly rather than
			// inventing one.
			task = "(untracked segment)"
		}
		base := fmt.Sprintf("cc_%s_%s", sessionID, seg.PromptID)
		for i, chunk := range split(seg.Steps) {
			externalID := base
			if len(seg.Steps) > len(chunk) {
				// Parts share the externalId prefix so the server can see they
				// belong together, while each stays independently idempotent.
				externalID = fmt.Sprintf("%s_p%d", base, i+1)
			}
			runs = append(runs, Run{
				ExternalID:     externalID,
				AgentName:      agentName,
				Task:           truncate(task, 8000),
				Outcome:        seg.Outcome,
				OutcomeSignals: seg.Signals,
				Harness:        "claude_code",
				StartedAt:      seg.OpenedAt,
				EndedAt:        seg.ClosedAt,
				Steps:          reindex(chunk),
				PromptID:       seg.PromptID,
			})
		}
	}
	return runs
}

func isTrivial(seg Segment, minSteps int) bool {
	if len(seg.Steps) < minSteps {
		return true
	}
	for _, s := range seg.Steps {
		if s.Type != TypeAssistantMessage {
			return false
		}
	}
	// Nothing but assistant text: the agent talked and did not act.
	return true
}

// TypeAssistantMessage is duplicated from steps to keep envelope free of a
// dependency cycle when steps grows a dependency on envelope.
const TypeAssistantMessage = "assistant_message"

// split chops a step list at a step boundary so no request exceeds either cap.
func split(all []Step) [][]Step {
	var chunks [][]Step
	cur := []Step{}
	curBytes := 0
	for _, s := range all {
		size := approxSize(s)
		overSteps := len(cur)+1 > MaxSteps
		overBytes := curBytes+size > MaxBodyBytes-bodyHeadroom
		if len(cur) > 0 && (overSteps || overBytes) {
			chunks = append(chunks, cur)
			cur, curBytes = []Step{}, 0
		}
		cur = append(cur, s)
		curBytes += size
	}
	if len(cur) > 0 {
		chunks = append(chunks, cur)
	}
	return chunks
}

func approxSize(s Step) int {
	b, err := json.Marshal(s)
	if err != nil {
		return 1024
	}
	return len(b) + 1
}

// reindex renumbers a chunk from 0. The server rejects any list whose indices are
// not contiguous from 0 in execution order (RunIngestRequest._steps_ordered), so
// a split part must be renumbered, not carry its offsets from the parent run.
func reindex(chunk []Step) []Step {
	out := make([]Step, len(chunk))
	for i, s := range chunk {
		s.Index = i
		out[i] = s
	}
	return out
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
