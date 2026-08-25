package handlers

import (
	"fmt"
	"os"
	"time"

	"github.com/brainite/plugin/internal/spool"
)

// Done records the user's confirmation that the current task succeeded.
//
// This is the highest-value signal in the whole loop and the cheapest to
// produce: human_confirmed bypasses the min_runs_per_cluster wait entirely
// (gate_run: `ready = human_confirmed or members >= …`), so one confirmed run is
// distillable where three inferred ones would otherwise be needed.
//
// The session is resolved from the spool rather than passed in, because the
// /brain-done skill runs as a model-issued shell command with no access to the
// hook payload's session_id. Matching on cwd first keeps two concurrent sessions
// in different repos from confirming each other's work.
func Done(cwd string) (string, error) {
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	paths, err := spool.List()
	if err != nil {
		return "", err
	}
	if len(paths) == 0 {
		return "", fmt.Errorf("no active Brainite session — is capture enabled for this repo?")
	}

	var bestID string
	var bestMod time.Time
	var fallbackID string
	var fallbackMod time.Time

	for _, p := range paths {
		fi, err := os.Stat(p)
		if err != nil {
			continue
		}
		records, err := spool.Read(p)
		if err != nil || len(records) == 0 {
			continue
		}
		sessionID, sessionCwd := "", ""
		for _, r := range records {
			if r.T == spool.KindSession {
				sessionID, sessionCwd = r.SessionID, r.Cwd
				break
			}
		}
		if sessionID == "" {
			continue
		}
		if fi.ModTime().After(fallbackMod) {
			fallbackID, fallbackMod = sessionID, fi.ModTime()
		}
		if sessionCwd == cwd && fi.ModTime().After(bestMod) {
			bestID, bestMod = sessionID, fi.ModTime()
		}
	}

	sessionID := bestID
	if sessionID == "" {
		sessionID = fallbackID
	}
	if sessionID == "" {
		return "", fmt.Errorf("no active Brainite session found")
	}
	return sessionID, WriteMarker(sessionID)
}
