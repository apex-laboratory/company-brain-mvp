// Package config reads ~/.brainite/{config.json,credentials}.
//
// Capture is **opt-in per repo**: a missing config, or a cwd not listed in
// EnabledRepos, makes every hook a no-op. Reading every prompt and shell command
// in a repo by default is a posture we deliberately don't copy from the
// competition — "we don't capture until you say so" is the product's position.
//
// The credential lives in ~/.brainite/credentials (0600) and never in
// .claude/settings.json, which people commit.
package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

const (
	// Bumped when the shape of a spooled record changes, so an old spool is
	// detected rather than silently misparsed by a newer flusher.
	SpoolVersion = 1
	// The golden-payload generation this build was written against.
	HookSchema = "2026-08"
)

type Inject struct {
	Enabled    bool `json:"enabled"`
	DeadlineMs int  `json:"deadline_ms"`
}

type Spool struct {
	MaxMB int `json:"max_mb"`
}

type Config struct {
	EnabledRepos []string `json:"enabled_repos"`
	AgentName    string   `json:"agent_name"`
	APIBaseURL   string   `json:"api_base_url"`
	MinSteps     int      `json:"min_steps"`
	TestCommands []string `json:"test_commands"`
	Inject       Inject   `json:"inject"`
	SpoolCfg     Spool    `json:"spool"`
	Redact       struct {
		ExtraPatterns []string `json:"extra_patterns"`
	} `json:"redact"`

	apiKey string
}

func Default() *Config {
	c := &Config{
		AgentName:  "claude-code",
		APIBaseURL: "http://localhost:8000/api/v1",
		// Matches settings.run_min_steps server-side: a shorter segment is
		// conversation, not a run, and would only be ingested to be rejected.
		MinSteps:     3,
		TestCommands: []string{"make test", "pytest", "npm test", "npm run test", "go test", "cargo test"},
		Inject:       Inject{Enabled: true, DeadlineMs: 800},
		SpoolCfg:     Spool{MaxMB: 50},
	}
	return c
}

// Dir is ~/.brainite, overridable with BRAINITE_HOME so tests never touch a
// developer's real spool.
func Dir() string {
	if d := os.Getenv("BRAINITE_HOME"); d != "" {
		return d
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".brainite"
	}
	return filepath.Join(home, ".brainite")
}

func Path() string      { return filepath.Join(Dir(), "config.json") }
func SpoolDir() string  { return filepath.Join(Dir(), "spool") }
func MarkerDir() string { return filepath.Join(Dir(), "marker") }
func CacheDir() string  { return filepath.Join(Dir(), "cache") }
func CredsPath() string { return filepath.Join(Dir(), "credentials") }

// Load merges the on-disk config over the defaults. A missing file is not an
// error — it yields defaults with no enabled repos, i.e. capture off.
func Load() (*Config, error) {
	c := Default()
	b, err := os.ReadFile(Path())
	if err == nil {
		if err := json.Unmarshal(b, c); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if c.AgentName == "" {
		c.AgentName = "claude-code"
	}
	if c.Inject.DeadlineMs <= 0 {
		c.Inject.DeadlineMs = 800
	}
	if c.MinSteps <= 0 {
		c.MinSteps = 3
	}
	if base := os.Getenv("BRAINITE_API_BASE"); base != "" {
		c.APIBaseURL = base
	}
	c.apiKey = loadKey()
	return c, nil
}

func loadKey() string {
	if k := os.Getenv("BRAINITE_API_KEY"); k != "" {
		return strings.TrimSpace(k)
	}
	b, err := os.ReadFile(CredsPath())
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (c *Config) APIKey() string { return c.apiKey }

// Save writes the config back with 0600 — it names the repos a developer works in.
func (c *Config) Save() error {
	if err := os.MkdirAll(Dir(), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(Path(), append(b, '\n'), 0o600)
}

// CaptureEnabled reports whether cwd is inside an opted-in repo. Matching is by
// path prefix so a subdirectory of an enabled repo is also captured.
func (c *Config) CaptureEnabled(cwd string) bool {
	abs, err := filepath.Abs(cwd)
	if err != nil {
		return false
	}
	abs = filepath.Clean(abs)
	for _, repo := range c.EnabledRepos {
		r := filepath.Clean(repo)
		if abs == r || strings.HasPrefix(abs, r+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// Enable opts a repo in, idempotently.
func (c *Config) Enable(repo string) error {
	abs, err := filepath.Abs(repo)
	if err != nil {
		return err
	}
	abs = filepath.Clean(abs)
	for _, r := range c.EnabledRepos {
		if filepath.Clean(r) == abs {
			return nil
		}
	}
	c.EnabledRepos = append(c.EnabledRepos, abs)
	return c.Save()
}
