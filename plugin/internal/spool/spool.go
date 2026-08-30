// Package spool is the append-only NDJSON journal every hot hook writes to.
//
// The whole architecture rests on one rule: a hook on the agent's critical path
// never touches the network. It appends a line here and returns. Assembly,
// redaction sweep and the POST happen out of band in the detached flusher.
//
// Files are opened O_APPEND so two hooks racing (parallel tool calls do overlap)
// can never interleave a partial write.
package spool

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/brainite/plugin/internal/config"
)

// Record kinds.
const (
	KindSession      = "session"
	KindSegmentOpen  = "segment_open"
	KindStepOpen     = "step_open"
	KindStepClose    = "step_close"
	KindStep         = "step"
	KindSegmentClose = "segment_close"
)

// Record is one spooled line. A single struct rather than a type per kind:
// the flusher reads a heterogeneous stream and switching on T is simpler than
// a discriminated decode, and the file stays greppable by a human debugging it.
type Record struct {
	V  int    `json:"v"`
	T  string `json:"t"`
	At string `json:"at,omitempty"`

	// session
	SessionID  string `json:"sessionId,omitempty"`
	Cwd        string `json:"cwd,omitempty"`
	AgentName  string `json:"agentName,omitempty"`
	HookSchema string `json:"hookSchema,omitempty"`

	// segment_open / segment_close — keyed by the platform's prompt_id
	PromptID string `json:"promptId,omitempty"`
	Task     string `json:"task,omitempty"`

	// step_open / step_close
	ToolUseID    string         `json:"toolUseId,omitempty"`
	Index        int            `json:"index"`
	Type         string         `json:"type,omitempty"`
	Name         string         `json:"name,omitempty"`
	Args         map[string]any `json:"args,omitempty"`
	Text         string         `json:"text,omitempty"`
	Status       string         `json:"status,omitempty"`
	ResultDigest string         `json:"resultDigest,omitempty"`
	LatencyMs    int            `json:"latencyMs,omitempty"`

	// segment_close
	Outcome        string          `json:"outcome,omitempty"`
	OutcomeSignals map[string]bool `json:"outcomeSignals,omitempty"`
}

func Path(sessionID string) string {
	return filepath.Join(config.SpoolDir(), sessionID+".ndjson")
}

// Append writes one record. It is the only writer, and it is O_APPEND.
func Append(sessionID string, r Record) error {
	if r.V == 0 {
		r.V = config.SpoolVersion
	}
	if r.At == "" {
		r.At = time.Now().UTC().Format(time.RFC3339Nano)
	}
	if err := os.MkdirAll(config.SpoolDir(), 0o700); err != nil {
		return err
	}
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(Path(sessionID), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(append(b, '\n'))
	return err
}

// Read returns every record in a spool file, skipping unparseable lines. A line
// we cannot parse is a bug we want to survive, not a reason to lose the session.
func Read(path string) ([]Record, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []Record
	sc := bufio.NewScanner(f)
	// Steps can carry a long command; the default 64KB token cap is too small.
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r Record
		if err := json.Unmarshal(line, &r); err != nil {
			continue
		}
		out = append(out, r)
	}
	return out, sc.Err()
}

// List returns every spool file, oldest first.
func List() ([]string, error) {
	entries, err := os.ReadDir(config.SpoolDir())
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var paths []string
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".ndjson" {
			continue
		}
		paths = append(paths, filepath.Join(config.SpoolDir(), e.Name()))
	}
	sort.Strings(paths)
	return paths, nil
}

// NextIndex counts the steps already opened in a segment, so PreToolUse can
// assign an index in *arrival* order. Parallel tool calls complete out of order
// (observed in the golden fixtures), so closing order would produce a shuffled
// list and the server's contiguous-order validator would 422 the whole run.
func NextIndex(records []Record, promptID string) int {
	n := 0
	for _, r := range records {
		if r.T == KindStepOpen && r.PromptID == promptID {
			n++
		}
	}
	return n
}

// SentPath is the sidecar marking which segments of a spool file have been
// accepted by the server. Written only after a 2xx, so a crash mid-flush
// re-sends and externalId makes the re-send idempotent.
func SentPath(path string) string { return path + ".sent" }

func ReadSent(path string) map[string]bool {
	sent := map[string]bool{}
	f, err := os.Open(SentPath(path))
	if err != nil {
		return sent
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if id := sc.Text(); id != "" {
			sent[id] = true
		}
	}
	return sent
}

func MarkSent(path, promptID string) error {
	f, err := os.OpenFile(SentPath(path), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.WriteString(promptID + "\n")
	return err
}
