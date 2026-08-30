package config

import (
	"os"
	"path/filepath"
	"testing"
)

// TestCaptureIsOptIn is the consent boundary. Capture off by default is a
// deliberate divergence from the competition, which writes by default and offers
// mute as an escape hatch. "We don't capture until you say so" is the product's
// position, so a bug that flips this default is a trust incident, not a feature
// regression.
func TestCaptureIsOptIn(t *testing.T) {
	t.Setenv("BRAINITE_HOME", t.TempDir())
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.CaptureEnabled("/some/repo") {
		t.Fatal("capture is on with no config — it must be opt-in per repo")
	}
	if len(cfg.EnabledRepos) != 0 {
		t.Errorf("EnabledRepos = %v, want empty", cfg.EnabledRepos)
	}
}

func TestEnableIsIdempotentAndScoped(t *testing.T) {
	home := t.TempDir()
	t.Setenv("BRAINITE_HOME", home)
	repo := t.TempDir()

	cfg, _ := Load()
	if err := cfg.Enable(repo); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Enable(repo); err != nil {
		t.Fatal(err)
	}
	if len(cfg.EnabledRepos) != 1 {
		t.Errorf("enabling twice produced %v", cfg.EnabledRepos)
	}

	reloaded, _ := Load()
	if !reloaded.CaptureEnabled(repo) {
		t.Error("enabled repo did not survive a reload")
	}
	// Subdirectories are inside the opted-in repo.
	if !reloaded.CaptureEnabled(filepath.Join(repo, "src", "deep")) {
		t.Error("a subdirectory of an enabled repo should be captured")
	}
	// A sibling whose path merely shares a prefix must not be.
	if reloaded.CaptureEnabled(repo + "-other") {
		t.Error("a path sharing a prefix with an enabled repo must not be captured")
	}
	if reloaded.CaptureEnabled(t.TempDir()) {
		t.Error("an unrelated repo must not be captured")
	}
}

// TestCredentialsAreNotWorldReadable — the key is a workspace-scoped credential
// on a shared machine.
func TestCredentialsFilePermissions(t *testing.T) {
	home := t.TempDir()
	t.Setenv("BRAINITE_HOME", home)
	cfg, _ := Load()
	if err := cfg.Enable(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(Path())
	if err != nil {
		t.Fatal(err)
	}
	if perm := fi.Mode().Perm(); perm != 0o600 {
		t.Errorf("config.json mode = %o, want 600 — it names the repos a developer works in", perm)
	}
}

// TestEnvKeyOverridesFile lets CI push without writing a credential to disk.
func TestEnvKeyOverridesFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("BRAINITE_HOME", home)
	if err := os.WriteFile(filepath.Join(home, "credentials"), []byte("from_file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, _ := Load()
	if cfg.APIKey() != "from_file" {
		t.Errorf("APIKey = %q, want the trimmed file contents", cfg.APIKey())
	}
	t.Setenv("BRAINITE_API_KEY", "from_env")
	cfg, _ = Load()
	if cfg.APIKey() != "from_env" {
		t.Errorf("APIKey = %q, want the env override", cfg.APIKey())
	}
}
