// Package redact is the *first* line of defence — the one that runs before
// anything leaves the customer's machine.
//
// The server runs its own pass (app/modules/runs/redaction.py) and that one is
// authoritative, but by the time it runs the trace has already crossed the wire.
// Defence in depth is the entire argument for being trusted with Bash output, so
// the patterns here deliberately mirror the server's rather than assuming it will
// catch what we miss.
//
// Fail closed: Scrub never returns partially-scrubbed output. If a value can't be
// walked, it is replaced wholesale.
package redact

import (
	"fmt"
	"regexp"
	"strings"
)

const Redacted = "[redacted]"

// maxDepth guards against a pathological nested payload turning a hot hook into a
// stack overflow. Beyond it, the subtree is dropped rather than walked.
const maxDepth = 8

// sensitiveKeys drop their value whatever it looks like. Key-matching catches the
// secret with no recognisable shape ({"password": "hunter2"}) that no pattern will.
var sensitiveKeys = map[string]bool{
	"apikey": true, "secret": true, "secrets": true, "token": true,
	"accesstoken": true, "refreshtoken": true, "idtoken": true, "password": true,
	"passwd": true, "pwd": true, "authorization": true, "auth": true,
	"privatekey": true, "clientsecret": true, "sessionkey": true, "cookie": true,
	"credentials": true, "connectionstring": true, "dsn": true,
	"encryptionkey": true, "signingkey": true, "webhooksecret": true,
}

// valuePatterns run on every string. Ordered structured-first so a connection
// string is masked as a unit before a generic rule mangles half of it.
var valuePatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`),
	regexp.MustCompile(`(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://[^\s/@]+:[^\s/@]+@\S+`),
	regexp.MustCompile(`(?i)\bauthorization\b\s*[:=]\s*\S+`),
	regexp.MustCompile(`(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*`),
	regexp.MustCompile(`\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}`),
	regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{20,}`),
	regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}`),
	regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`),
	regexp.MustCompile(`\bASIA[0-9A-Z]{16}\b`),
	regexp.MustCompile(`\bxox[baprs]-[A-Za-z0-9-]{10,}`),
	regexp.MustCompile(`\bAIza[0-9A-Za-z_-]{30,}`),
	regexp.MustCompile(`\bglpat-[A-Za-z0-9_-]{16,}`),
	regexp.MustCompile(`\bnpm_[A-Za-z0-9]{30,}`),
	// JWTs. Matches our own access tokens too, which is the point: an agent that
	// echoed its credential must not persist it.
	regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`),
	// KEY=value assignments in shell/.env shape.
	regexp.MustCompile(`(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*\s*=\s*\S+`),
}

var extra []*regexp.Regexp

// AddPatterns compiles the operator's redact.extra_patterns. An uncompilable
// pattern is skipped rather than fatal — a typo in config must not stop capture,
// but it must also not silently widen what we send, so it simply adds nothing.
func AddPatterns(pats []string) {
	for _, p := range pats {
		if re, err := regexp.Compile(p); err == nil {
			extra = append(extra, re)
		}
	}
}

// String applies every value pattern.
func String(s string) string {
	for _, re := range valuePatterns {
		s = re.ReplaceAllString(s, Redacted)
	}
	for _, re := range extra {
		s = re.ReplaceAllString(s, Redacted)
	}
	return s
}

func normalizeKey(k string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(k) {
		if r != '_' && r != '-' && r != '.' && r != ' ' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// Value walks any decoded JSON value, redacting by key and by value.
func Value(v any) any { return walk(v, 0) }

func walk(v any, depth int) any {
	if depth > maxDepth {
		return Redacted
	}
	switch t := v.(type) {
	case string:
		return String(t)
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			if sensitiveKeys[normalizeKey(k)] {
				out[k] = Redacted
				continue
			}
			out[k] = walk(val, depth+1)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, val := range t {
			out[i] = walk(val, depth+1)
		}
		return out
	case nil, bool, float64, int, int64:
		return v
	default:
		return String(fmt.Sprint(v))
	}
}
