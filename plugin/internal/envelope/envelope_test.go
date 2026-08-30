package envelope

import (
	"encoding/json"
	"fmt"
	"testing"

	"github.com/brainite/plugin/internal/spool"
)

func session(id, cwd string) spool.Record {
	return spool.Record{T: spool.KindSession, SessionID: id, Cwd: cwd, AgentName: "claude-code"}
}
func segOpen(promptID, task string) spool.Record {
	return spool.Record{T: spool.KindSegmentOpen, PromptID: promptID, Task: task}
}
func segClose(promptID, outcome string) spool.Record {
	return spool.Record{T: spool.KindSegmentClose, PromptID: promptID, Outcome: outcome}
}
func stepOpen(promptID, toolUseID string, idx int, typ, name string) spool.Record {
	return spool.Record{T: spool.KindStepOpen, PromptID: promptID, ToolUseID: toolUseID, Index: idx, Type: typ, Name: name}
}
func stepClose(promptID, toolUseID, status string) spool.Record {
	return spool.Record{T: spool.KindStepClose, PromptID: promptID, ToolUseID: toolUseID, Status: status, ResultDigest: "abc bytes=10 lines=1"}
}

// TestUnclosedStepBecomesError is the finding that drives the whole PreToolUse
// design: a tool call that *fails* emits PreToolUse and no PostToolUse at all
// (testdata/golden/README.md §1). A step opened and never closed is therefore the
// only evidence a failure happened, and if it were assumed successful every run
// in the corpus would look clean.
func TestUnclosedStepBecomesError(t *testing.T) {
	records := []spool.Record{
		session("s1", "/repo"),
		segOpen("p1", "do the thing"),
		stepOpen("p1", "t1", 0, "file_read", "a.go"),
		stepClose("p1", "t1", "ok"),
		stepOpen("p1", "t2", 1, "shell", "cat /nope"), // never closed: it failed
		stepOpen("p1", "t3", 2, "file_read", "b.go"),
		stepClose("p1", "t3", "ok"),
		segClose("p1", "ambiguous"),
	}
	_, _, _, segments := Assemble(records)
	if len(segments) != 1 {
		t.Fatalf("segments = %d, want 1", len(segments))
	}
	steps := segments[0].Steps
	if len(steps) != 3 {
		t.Fatalf("steps = %d, want 3", len(steps))
	}
	if steps[1].Status != "error" {
		t.Errorf("unclosed step status = %q, want error — failures would be invisible", steps[1].Status)
	}
	if steps[0].Status != "ok" || steps[2].Status != "ok" {
		t.Errorf("closed steps should be ok, got %q and %q", steps[0].Status, steps[2].Status)
	}
}

// TestOutOfOrderCompletionKeepsExecutionOrder — parallel tool calls complete out
// of order (observed in the fixtures). Ordering by close time would ship a
// procedure whose steps are in the wrong order, which is worse than none.
func TestOutOfOrderCompletionKeepsExecutionOrder(t *testing.T) {
	records := []spool.Record{
		session("s1", "/repo"),
		segOpen("p1", "task"),
		stepOpen("p1", "t1", 0, "shell", "slow"),
		stepOpen("p1", "t2", 1, "tool_call", "Grep"),
		stepClose("p1", "t2", "ok"), // the later call closes first
		stepClose("p1", "t1", "ok"),
		stepOpen("p1", "t3", 2, "file_write", "c.go"),
		stepClose("p1", "t3", "ok"),
		segClose("p1", "success"),
	}
	_, _, _, segments := Assemble(records)
	names := []string{}
	for _, s := range segments[0].Steps {
		names = append(names, s.Name)
	}
	want := []string{"slow", "Grep", "c.go"}
	for i := range want {
		if names[i] != want[i] {
			t.Fatalf("step order = %v, want %v", names, want)
		}
	}
}

// TestOpenSegmentIsNotShipped — a segment with no close is still in flight.
// Shipping half a run now would cost the idempotency of the eventual full one.
func TestOpenSegmentIsNotShipped(t *testing.T) {
	records := []spool.Record{
		session("s1", "/repo"),
		segOpen("p1", "closed"), stepOpen("p1", "t1", 0, "shell", "x"), stepClose("p1", "t1", "ok"),
		segClose("p1", "success"),
		segOpen("p2", "still going"), stepOpen("p2", "t2", 0, "shell", "y"),
	}
	_, _, _, segments := Assemble(records)
	if len(segments) != 1 || segments[0].PromptID != "p1" {
		t.Fatalf("expected only the closed segment, got %d: %+v", len(segments), segments)
	}
}

// TestExternalIDIsStable — the server UPSERTs on (workspace_id, external_id), so
// this key is what makes a re-flushed spool idempotent rather than duplicated.
func TestExternalIDIsStable(t *testing.T) {
	seg := Segment{PromptID: "p1", Task: "t", Outcome: "success", Steps: threeSteps()}
	a := Build("sess", "claude-code", []Segment{seg}, 3)
	b := Build("sess", "claude-code", []Segment{seg}, 3)
	if a[0].ExternalID != b[0].ExternalID {
		t.Fatalf("externalId not stable: %q vs %q", a[0].ExternalID, b[0].ExternalID)
	}
	if a[0].ExternalID != "cc_sess_p1" {
		t.Errorf("externalId = %q, want cc_sess_p1", a[0].ExternalID)
	}
}

// TestTrivialSegmentsDropped — a short segment with no tool call is
// conversation. Sending it costs a request to be told "too_trivial".
func TestTrivialSegmentsDropped(t *testing.T) {
	chat := Segment{PromptID: "p1", Task: "hello", Outcome: "ambiguous", Steps: []Step{
		{Index: 0, Type: TypeAssistantMessage, Status: "ok"},
	}}
	if runs := Build("s", "a", []Segment{chat}, 3); len(runs) != 0 {
		t.Fatalf("trivial segment produced %d runs, want 0", len(runs))
	}
	allTalk := Segment{PromptID: "p2", Task: "hi", Outcome: "ambiguous", Steps: []Step{
		{Index: 0, Type: TypeAssistantMessage, Status: "ok"},
		{Index: 1, Type: TypeAssistantMessage, Status: "ok"},
		{Index: 2, Type: TypeAssistantMessage, Status: "ok"},
	}}
	if runs := Build("s", "a", []Segment{allTalk}, 3); len(runs) != 0 {
		t.Fatalf("assistant-only segment produced %d runs, want 0", len(runs))
	}
}

// TestSplitReindexesFromZero — the server rejects any step list whose indices are
// not contiguous from 0 in execution order, so a split part must be renumbered
// rather than carry its offsets from the parent run.
func TestSplitReindexesFromZero(t *testing.T) {
	var steps []Step
	for i := 0; i < MaxSteps+50; i++ {
		steps = append(steps, Step{Index: i, Type: "shell", Name: fmt.Sprintf("cmd-%d", i), Status: "ok"})
	}
	seg := Segment{PromptID: "p1", Task: "big", Outcome: "success", Steps: steps}
	runs := Build("s", "a", []Segment{seg}, 3)
	if len(runs) < 2 {
		t.Fatalf("expected a split, got %d run(s)", len(runs))
	}
	for _, run := range runs {
		if len(run.Steps) > MaxSteps {
			t.Errorf("%s has %d steps, over the %d cap", run.ExternalID, len(run.Steps), MaxSteps)
		}
		for i, s := range run.Steps {
			if s.Index != i {
				t.Fatalf("%s step %d has index %d — the server would 422 this", run.ExternalID, i, s.Index)
			}
		}
		body, _ := json.Marshal(run)
		if len(body) > MaxBodyBytes {
			t.Errorf("%s serialises to %d bytes, over the %d cap", run.ExternalID, len(body), MaxBodyBytes)
		}
	}
	if runs[0].ExternalID == runs[1].ExternalID {
		t.Error("split parts must have distinct externalIds or they overwrite each other")
	}
}

func threeSteps() []Step {
	return []Step{
		{Index: 0, Type: "file_read", Name: "a.go", Status: "ok"},
		{Index: 1, Type: "shell", Name: "make test", Status: "ok"},
		{Index: 2, Type: "file_write", Name: "b.go", Status: "ok"},
	}
}
