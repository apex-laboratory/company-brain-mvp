package handlers

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/brainite/plugin/internal/api"
	"github.com/brainite/plugin/internal/config"
	"github.com/brainite/plugin/internal/envelope"
	"github.com/brainite/plugin/internal/hookio"
	"github.com/brainite/plugin/internal/spool"
)

const (
	flushAttempts = 3
	flushBackoff  = 2 * time.Second
	// A spool file with everything sent is kept a day, long enough to debug a
	// session that behaved oddly, then removed.
	spoolRetention = 24 * time.Hour
)

// Flush assembles closed segments and pushes them.
//
// Runs detached, off the agent's critical path. Order matters: a segment is
// marked sent only *after* a 2xx, so a crash mid-flush re-sends it and the
// server's UPSERT on (workspace_id, external_id) makes that harmless.
func Flush(_ []byte) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.APIKey() == "" {
		// Nothing to do, and nothing lost: the spool keeps accumulating and
		// drains once a credential exists.
		hookio.Debugf("flush: no API key; leaving spool in place")
		return nil
	}
	release, ok := acquireFlushLock()
	if !ok {
		hookio.Debugf("flush: another flusher holds the lock; nothing to do")
		return nil
	}
	defer release()

	client := api.New(cfg.APIBaseURL, cfg.APIKey())

	paths, err := spool.List()
	if err != nil {
		return err
	}
	enforceSpoolCap(paths, cfg.SpoolCfg.MaxMB)

	for _, path := range paths {
		flushFile(client, cfg, path)
	}
	return nil
}

func flushFile(client *api.Client, cfg *config.Config, path string) {
	records, err := spool.Read(path)
	if err != nil {
		hookio.Debugf("flush: read %s: %v", path, err)
		return
	}
	sessionID, _, agentName, segments := envelope.Assemble(records)
	if sessionID == "" {
		sessionID = trimName(path)
	}
	if agentName == "" {
		agentName = cfg.AgentName
	}

	sent := spool.ReadSent(path)
	runs := envelope.Build(sessionID, agentName, segments, cfg.MinSteps)

	allSent := true
	for _, run := range runs {
		if sent[run.ExternalID] {
			continue
		}
		if pushWithRetry(client, run) {
			if err := spool.MarkSent(path, run.ExternalID); err != nil {
				hookio.Debugf("flush: mark sent: %v", err)
			}
		} else {
			// Left in the spool for the next session's flush. Offline for a week
			// means the spool drains when connectivity returns.
			allSent = false
		}
	}

	if allSent && len(runs) > 0 {
		if fi, err := os.Stat(path); err == nil && time.Since(fi.ModTime()) > spoolRetention {
			_ = os.Remove(path)
			_ = os.Remove(spool.SentPath(path))
		}
	}
}

func pushWithRetry(client *api.Client, run envelope.Run) bool {
	backoff := flushBackoff
	for attempt := 1; attempt <= flushAttempts; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		res, retryable, err := client.PostRun(ctx, run)
		cancel()
		if err == nil {
			if res != nil && res.Duplicate {
				hookio.Debugf("flush: %s already ingested", run.ExternalID)
			}
			return true
		}
		if !retryable {
			// A 4xx will never become a 2xx. Marking it sent stops the spool
			// pinning on an envelope the server has permanently refused.
			hookio.Debugf("flush: %s rejected permanently: %v", run.ExternalID, err)
			return true
		}
		hookio.Debugf("flush: %s attempt %d failed: %v", run.ExternalID, attempt, err)
		if attempt < flushAttempts {
			time.Sleep(backoff)
			backoff *= 2
		}
	}
	return false
}

// enforceSpoolCap drops the oldest files once the spool exceeds its budget.
// Unbounded local growth on a developer's machine is a support ticket.
func enforceSpoolCap(paths []string, maxMB int) {
	if maxMB <= 0 {
		return
	}
	limit := int64(maxMB) * 1024 * 1024
	type entry struct {
		path string
		mod  time.Time
		size int64
	}
	var entries []entry
	var total int64
	for _, p := range paths {
		fi, err := os.Stat(p)
		if err != nil {
			continue
		}
		entries = append(entries, entry{p, fi.ModTime(), fi.Size()})
		total += fi.Size()
	}
	if total <= limit {
		return
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].mod.Before(entries[j].mod) })
	for _, e := range entries {
		if total <= limit {
			return
		}
		hookio.Debugf("flush: spool over %dMB, dropping %s", maxMB, e.path)
		_ = os.Remove(e.path)
		_ = os.Remove(spool.SentPath(e.path))
		total -= e.size
	}
}

func trimName(path string) string {
	base := filepath.Base(path)
	return base[:len(base)-len(filepath.Ext(base))]
}
