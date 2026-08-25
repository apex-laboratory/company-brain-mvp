package handlers

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/brainite/plugin/internal/api"
	"github.com/brainite/plugin/internal/config"
	"github.com/brainite/plugin/internal/hookio"
)

const (
	// A prompt shorter than this is a continuation, not a task worth a lookup.
	minPromptChars = 25
	// Mirrors the server-side read cache, so a repeated prompt costs nothing.
	injectCacheTTL = 5 * time.Minute
	// Below this the match is noise. The server publishes at 0.70 for query_brain;
	// injecting a weak hit unasked is worse than injecting nothing.
	minSimilarity = 0.70
)

// continuations are the refinement turns that would otherwise triple our request
// volume for no benefit — the previous turn's injection still applies.
var continuations = map[string]bool{
	"continue": true, "yes": true, "y": true, "no": true, "go on": true,
	"go ahead": true, "ok": true, "okay": true, "thanks": true, "next": true,
	"proceed": true, "keep going": true, "do it": true,
}

// injectFor returns the additionalContext for a prompt, or "" for silence.
//
// Hard deadline from config (800ms by default) against a platform timeout of 30s.
// The platform's generosity is not permission to use it: this runs between the
// user pressing enter and the agent starting work.
//
// Every failure path returns "". A lookup that fails must be invisible — an error
// string in the agent's context is worse than no context at all.
func injectFor(cfg *config.Config, prompt string) string {
	trimmed := strings.TrimSpace(prompt)
	if len(trimmed) < minPromptChars {
		return ""
	}
	if continuations[strings.ToLower(strings.Trim(trimmed, ".!? "))] {
		return ""
	}
	if cfg.APIKey() == "" {
		return ""
	}

	key := hashPrompt(trimmed)
	if cached, ok := readCache(key); ok {
		return cached
	}

	ctx, cancel := context.WithTimeout(context.Background(),
		time.Duration(cfg.Inject.DeadlineMs)*time.Millisecond)
	defer cancel()

	hits, err := api.New(cfg.APIBaseURL, cfg.APIKey()).SearchSkills(ctx, trimmed, 1)
	if err != nil {
		hookio.Debugf("inject: %v", err)
		return ""
	}
	if len(hits) == 0 || hits[0].Similarity < minSimilarity {
		// Cache the miss too: re-asking the same unmatched prompt costs the same
		// round-trip and returns the same nothing.
		writeCache(key, "")
		return ""
	}

	out := render(hits[0])
	writeCache(key, out)
	return out
}

// render always carries the provenance line. Silent injection — text appearing in
// the agent's context with no attribution — is a trust liability, not a feature:
// the developer has to be able to see what the brain claimed and judge it.
func render(h api.SkillHit) string {
	var b strings.Builder
	fmt.Fprintf(&b, "[Brainite] %s v%s · match %.2f", h.Name, h.Version, h.Similarity)
	if h.SourceAuthority != "" {
		fmt.Fprintf(&b, " · source: %s", h.SourceAuthority)
	}
	b.WriteString("\nYour company has a reviewed procedure for this. Follow it unless it conflicts with the user's explicit instruction; if you override it, say so.\n\n")
	b.WriteString(h.BaseLogic)
	if len(h.ExceptionsBlock) > 0 {
		fmt.Fprintf(&b, "\n\nExceptions (%d) — check these before acting.", len(h.ExceptionsBlock))
	}
	return b.String()
}

func hashPrompt(p string) string {
	sum := sha256.Sum256([]byte(p))
	return hex.EncodeToString(sum[:])[:32]
}

func cachePath(key string) string { return filepath.Join(config.CacheDir(), "q_"+key) }

func readCache(key string) (string, bool) {
	fi, err := os.Stat(cachePath(key))
	if err != nil || time.Since(fi.ModTime()) > injectCacheTTL {
		return "", false
	}
	b, err := os.ReadFile(cachePath(key))
	if err != nil {
		return "", false
	}
	return string(b), true
}

func writeCache(key, val string) {
	if err := os.MkdirAll(config.CacheDir(), 0o700); err != nil {
		return
	}
	_ = os.WriteFile(cachePath(key), []byte(val), 0o600)
}
