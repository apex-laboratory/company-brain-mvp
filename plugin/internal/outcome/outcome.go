// Package outcome resolves whether a run segment succeeded.
//
// Claude Code emits no success signal, and getting this wrong in the optimistic
// direction poisons the corpus with procedures distilled from runs that failed.
// So: ranked signals, first match wins, and **ambiguous is the default**.
//
// Ambiguous runs are emitted, not dropped. The server owns the gate
// (gate_run.evaluate excludes anything that isn't "success"), and ineligible runs
// are a tracked product signal. The client's job is honest labelling, not
// eligibility.
package outcome

import (
	"strings"

	"github.com/brainite/plugin/internal/spool"
	"github.com/brainite/plugin/internal/steps"
)

// tailSteps mirrors the server's _TAIL_STEPS: the gate re-checks the last three
// steps for errors, so a client that labels success over a failing tail is only
// buying itself a rejection.
const tailSteps = 3

type Result struct {
	Outcome string
	Signals map[string]bool
}

const (
	Success   = "success"
	Failure   = "failure"
	Ambiguous = "ambiguous"
)

// Resolve ranks the evidence for one segment's steps.
//
// Rank 1 of the spec ("the model called report_run") is unreachable: no such MCP
// tool exists server-side. Ranks 2-6 carry v1, and /brain-done — rank 2 — is both
// the strongest signal and the cheapest, since it bypasses the cluster-size wait.
func Resolve(stepList []spool.Record, testCommands []string, humanConfirmed bool) Result {
	sig := map[string]bool{}

	tailClean := tailIsClean(stepList)
	sig["errorFreeTail"] = tailClean

	// Rank 2 — a person said so. The only non-inferred signal we have.
	if humanConfirmed {
		sig["humanConfirmed"] = true
		return Result{Outcome: Success, Signals: sig}
	}

	// Rank 3 — a test command ran and nothing that looked like a test failed.
	ranTest, testFailed := scanTests(stepList, testCommands)
	if ranTest && !testFailed {
		sig["testsPassed"] = true
		sig["inferred"] = true
		return Result{Outcome: Success, Signals: sig}
	}
	if testFailed {
		sig["testsPassed"] = false
	}

	// Rank 4 — a commit landed. Weaker than a green test run but still an act
	// the developer chose to take on the result.
	if committed(stepList) && !testFailed {
		sig["inferred"] = true
		return Result{Outcome: Success, Signals: sig}
	}

	// Rank 5 — the evidence says it ended badly.
	if !tailClean {
		sig["inferred"] = true
		return Result{Outcome: Failure, Signals: sig}
	}

	// Rank 6 — we genuinely cannot tell, and saying so is the honest answer.
	sig["inferred"] = true
	return Result{Outcome: Ambiguous, Signals: sig}
}

func tailIsClean(stepList []spool.Record) bool {
	start := len(stepList) - tailSteps
	if start < 0 {
		start = 0
	}
	for _, s := range stepList[start:] {
		if s.Status == steps.StatusError {
			return false
		}
	}
	return true
}

// scanTests looks for a configured test invocation among the shell steps. The
// list is explicit rather than inferred: deciding "was that a test?" from
// arbitrary shell is not worth an LLM call on the client, and a wrong guess here
// asserts success.
func scanTests(stepList []spool.Record, testCommands []string) (ran, failed bool) {
	for _, s := range stepList {
		if s.Type != steps.TypeShell {
			continue
		}
		cmd := strings.ToLower(s.Name)
		for _, t := range testCommands {
			if t == "" || !strings.Contains(cmd, strings.ToLower(t)) {
				continue
			}
			ran = true
			if s.Status == steps.StatusError {
				failed = true
			}
			break
		}
	}
	return ran, failed
}

func committed(stepList []spool.Record) bool {
	for _, s := range stepList {
		if s.Type != steps.TypeShell || s.Status == steps.StatusError {
			continue
		}
		cmd := strings.ToLower(s.Name)
		if strings.Contains(cmd, "git commit") {
			return true
		}
	}
	return false
}
