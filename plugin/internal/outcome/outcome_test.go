package outcome

import "testing"

import "github.com/brainite/plugin/internal/spool"

func shell(cmd, status string) spool.Record {
	return spool.Record{T: spool.KindStepOpen, Type: "shell", Name: cmd, Status: status}
}
func tool(name, status string) spool.Record {
	return spool.Record{T: spool.KindStepOpen, Type: "tool_call", Name: name, Status: status}
}

var testCmds = []string{"make test", "pytest", "npm test"}

// TestResolveRanks pins the ranked table. Getting this wrong in the optimistic
// direction poisons the corpus with procedures distilled from failed runs, so
// each rank is asserted with the ranks above it deliberately absent.
func TestResolveRanks(t *testing.T) {
	cases := []struct {
		name            string
		steps           []spool.Record
		humanConfirmed  bool
		wantOutcome     string
		wantSignal      string
		wantSignalValue bool
	}{
		{
			name:            "rank 2: a human confirmed it",
			steps:           []spool.Record{shell("rm -rf /", "error")},
			humanConfirmed:  true,
			wantOutcome:     Success,
			wantSignal:      "humanConfirmed",
			wantSignalValue: true,
		},
		{
			name:            "rank 3: tests ran green",
			steps:           []spool.Record{tool("Read", "ok"), shell("pytest -q", "ok"), tool("Edit", "ok")},
			wantOutcome:     Success,
			wantSignal:      "testsPassed",
			wantSignalValue: true,
		},
		{
			name:        "rank 3 negated: a failing test in the tail is a failure, not a success",
			steps:       []spool.Record{tool("Read", "ok"), shell("pytest -q", "error"), tool("Read", "ok")},
			wantOutcome: Failure,
			wantSignal:  "testsPassed", wantSignalValue: false,
		},
		{
			name:            "rank 4: a commit landed",
			steps:           []spool.Record{tool("Edit", "ok"), shell("git commit -m x", "ok"), tool("Read", "ok")},
			wantOutcome:     Success,
			wantSignal:      "inferred",
			wantSignalValue: true,
		},
		{
			name:        "rank 5: the tail errored",
			steps:       []spool.Record{tool("Read", "ok"), tool("Read", "ok"), shell("ls", "error")},
			wantOutcome: Failure,
			wantSignal:  "errorFreeTail", wantSignalValue: false,
		},
		{
			name:        "rank 6: nothing to go on",
			steps:       []spool.Record{tool("Read", "ok"), tool("Grep", "ok"), tool("Read", "ok")},
			wantOutcome: Ambiguous,
			wantSignal:  "inferred", wantSignalValue: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Resolve(tc.steps, testCmds, tc.humanConfirmed)
			if got.Outcome != tc.wantOutcome {
				t.Errorf("outcome = %q, want %q (signals %v)", got.Outcome, tc.wantOutcome, got.Signals)
			}
			if v, ok := got.Signals[tc.wantSignal]; !ok || v != tc.wantSignalValue {
				t.Errorf("signal %s = %v (present=%v), want %v", tc.wantSignal, v, ok, tc.wantSignalValue)
			}
		})
	}
}

// TestAmbiguousIsTheDefault — the client's job is honest labelling, not
// eligibility. A run we cannot judge must say so rather than guess, because the
// server's gate is the single place that decides what distils.
func TestAmbiguousIsTheDefault(t *testing.T) {
	got := Resolve(nil, testCmds, false)
	if got.Outcome != Ambiguous {
		t.Fatalf("empty run resolved to %q, want %q", got.Outcome, Ambiguous)
	}
	if !got.Signals["inferred"] {
		t.Error("an inferred verdict must be marked inferred — it caps the resulting skill's authority")
	}
}

// TestHumanConfirmedIsNeverInferred — human_confirmed bypasses the cluster-size
// wait entirely, so it must never be set alongside a claim that we derived it.
func TestHumanConfirmedIsNeverInferred(t *testing.T) {
	got := Resolve([]spool.Record{shell("pytest", "ok")}, testCmds, true)
	if got.Signals["inferred"] {
		t.Error("humanConfirmed must not be marked inferred: a person actually said so")
	}
}

// TestCommitAfterFailingTests — a commit is weaker evidence than a red test run.
// Claiming success here would distil a procedure from work the tests reject.
func TestCommitAfterFailingTests(t *testing.T) {
	// The failing test sits outside the 3-step tail, so rank 5 does not catch it;
	// only rank 4's own guard stops the commit from asserting success.
	steps := []spool.Record{shell("pytest", "error"), tool("Read", "ok"), shell("git commit -m wip", "ok"), tool("Read", "ok")}
	if got := Resolve(steps, testCmds, false); got.Outcome == Success {
		t.Errorf("committed over failing tests resolved to success; want not-success (got %v)", got.Signals)
	}
}

// TestTailWindowMatchesServer — the server re-checks the last three steps and
// rejects a "success" whose tail errored. A client that labels outside that
// window only earns itself a rejection.
func TestTailWindowMatchesServer(t *testing.T) {
	// Error four steps back: outside the window, so the tail is clean.
	steps := []spool.Record{shell("boom", "error"), tool("Read", "ok"), tool("Read", "ok"), tool("Read", "ok")}
	if got := Resolve(steps, testCmds, false); !got.Signals["errorFreeTail"] {
		t.Error("an error outside the 3-step tail should leave errorFreeTail true")
	}
}
