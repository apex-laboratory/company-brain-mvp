package handlers

import (
	"os"
	"path/filepath"
	"time"

	"github.com/brainite/plugin/internal/config"
)

// briefTTL — a stale brief is worse than none: it would tell the agent the brain
// says something it no longer says.
const briefTTL = 24 * time.Hour

func briefPath() string { return filepath.Join(config.CacheDir(), "brief.txt") }

// cachedBrief reads the orientation blurb written opportunistically by the last
// flush. Disk only, never a network call — SessionStart is on the critical path,
// and this is the least important of the two injection surfaces. Empty on miss.
func cachedBrief() string {
	fi, err := os.Stat(briefPath())
	if err != nil || time.Since(fi.ModTime()) > briefTTL {
		return ""
	}
	b, err := os.ReadFile(briefPath())
	if err != nil {
		return ""
	}
	return string(b)
}
