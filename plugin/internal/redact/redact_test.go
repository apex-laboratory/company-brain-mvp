package redact

import (
	"encoding/json"
	"strings"
	"testing"
)

// seeded is the leak-test corpus: one credential of every shape we claim to
// catch. The client pass is the *first* line of defence — by the time the
// server's pass runs, the trace has already crossed the wire.
var seeded = map[string]string{
	"anthropic key":  "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
	"openai key":     "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
	"github pat":     "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
	"github fine":    "github_pat_11ABCDEFG0abcdefghijklmnop",
	"aws access key": "AKIAIOSFODNN7EXAMPLE",
	"slack token":    "xoxb-123456789012-abcdefghijkl",
	"google api key": "AIzaSyA1234567890abcdefghijklmnopqrstuv",
	"gitlab pat":     "glpat-abcdefghij1234567890",
	"npm token":      "npm_abcdefghijklmnopqrstuvwxyz0123456789",
	"jwt":            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
	"postgres dsn":   "postgres://admin:hunter2@db.internal:5432/prod",
	"auth header":    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
	"env assignment": "STRIPE_SECRET_KEY=sk_live_abcdefghijklmnop",
}

// TestSeededSecretsNeverSurvive is the leak test. Every value above is fed
// through the string path and must not come back out.
func TestSeededSecretsNeverSurvive(t *testing.T) {
	for name, secret := range seeded {
		t.Run(name, func(t *testing.T) {
			line := "some log output " + secret + " and more after it"
			got := String(line)
			if strings.Contains(got, secret) {
				t.Errorf("%s survived redaction: %q", name, got)
			}
			if !strings.Contains(got, Redacted) {
				t.Errorf("%s produced no redaction marker: %q", name, got)
			}
		})
	}
}

// TestSensitiveKeysDropByName catches the secret with no recognisable shape.
// No pattern will ever match "hunter2"; only the key tells us what it is.
func TestSensitiveKeysDropByName(t *testing.T) {
	in := map[string]any{
		"password":     "hunter2",
		"API_KEY":      "plainlookingvalue",
		"api-key":      "another",
		"clientSecret": "third",
		"harmless":     "keep me",
		"nested":       map[string]any{"token": "abc123", "fine": "also keep"},
	}
	out, ok := Value(in).(map[string]any)
	if !ok {
		t.Fatal("Value did not return a map")
	}
	for _, k := range []string{"password", "API_KEY", "api-key", "clientSecret"} {
		if out[k] != Redacted {
			t.Errorf("%s = %v, want %s", k, out[k], Redacted)
		}
	}
	if out["harmless"] != "keep me" {
		t.Errorf("harmless value was mangled: %v", out["harmless"])
	}
	nested := out["nested"].(map[string]any)
	if nested["token"] != Redacted {
		t.Errorf("nested token survived: %v", nested["token"])
	}
	if nested["fine"] != "also keep" {
		t.Errorf("nested harmless value was mangled: %v", nested["fine"])
	}
}

// TestDeepNestingIsBounded — a hostile or pathological payload must not turn a
// hot hook into a stack overflow.
func TestDeepNestingIsBounded(t *testing.T) {
	var v any = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"
	for i := 0; i < 200; i++ {
		v = map[string]any{"a": v}
	}
	out := Value(v)
	b, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "sk-ant-api03") {
		t.Error("a secret buried past the depth limit survived")
	}
}

// TestConnectionStringMaskedWhole — the structured patterns run first so a DSN is
// masked as a unit instead of being half-mangled by the generic assignment rule.
func TestConnectionStringMaskedWhole(t *testing.T) {
	got := String("psql postgres://admin:hunter2@db:5432/prod -c 'select 1'")
	if strings.Contains(got, "hunter2") || strings.Contains(got, "admin") {
		t.Errorf("DSN credentials survived: %q", got)
	}
}

// TestExtraPatternsCompileSafely — a typo in redact.extra_patterns must not stop
// capture, and must not silently widen what we send either.
func TestExtraPatternsCompileSafely(t *testing.T) {
	AddPatterns([]string{"([unclosed", "ACME-[0-9]{4}"})
	got := String("ticket ACME-1234 filed")
	if strings.Contains(got, "ACME-1234") {
		t.Errorf("valid extra pattern was not applied: %q", got)
	}
}
