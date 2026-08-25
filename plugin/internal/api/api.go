// Package api is the only package that touches the network, and it is only ever
// called from the detached flusher or from the deadlined injection path.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	BaseURL string
	APIKey  string
	HTTP    *http.Client
}

func New(baseURL, apiKey string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		APIKey:  apiKey,
		HTTP:    &http.Client{Timeout: 30 * time.Second},
	}
}

// RunAccepted is the 202 receipt. Duplicate distinguishes an idempotent re-push
// from a first ingest, which is how a retrying flusher knows to stop.
type RunAccepted struct {
	RunID     string `json:"runId"`
	Duplicate bool   `json:"duplicate"`
}

type envelope struct {
	Data json.RawMessage `json:"data"`
}

// PostRun pushes one run. The error distinguishes retryable from terminal:
// a 4xx means this envelope will never be accepted and re-sending it forever
// would pin the spool, so the caller drops it.
func (c *Client) PostRun(ctx context.Context, body any) (*RunAccepted, bool, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return nil, false, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/runs", bytes.NewReader(b))
	if err != nil {
		return nil, false, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", c.APIKey)

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, true, err // network failure: retryable
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	switch {
	case resp.StatusCode == http.StatusAccepted || resp.StatusCode == http.StatusOK:
		var env envelope
		out := &RunAccepted{}
		if err := json.Unmarshal(raw, &env); err == nil && len(env.Data) > 0 {
			_ = json.Unmarshal(env.Data, out)
		} else {
			_ = json.Unmarshal(raw, out)
		}
		return out, false, nil
	case resp.StatusCode == http.StatusTooManyRequests, resp.StatusCode >= 500:
		return nil, true, fmt.Errorf("POST /runs: %s", resp.Status)
	default:
		// 400/401/403/413/422 — the envelope is wrong or the credential is.
		// Retrying cannot fix either.
		return nil, false, fmt.Errorf("POST /runs: %s: %s", resp.Status, snippet(raw))
	}
}

// SkillHit is one result from GET /skills/search.
type SkillHit struct {
	ID              string  `json:"id"`
	Name            string  `json:"name"`
	Version         string  `json:"version"`
	BaseLogic       string  `json:"baseLogic"`
	SourceAuthority string  `json:"sourceAuthority"`
	Similarity      float64 `json:"similarity"`
	ExceptionsBlock []any   `json:"exceptionsBlock"`
}

// SearchSkills is the injection lookup.
//
// Deliberately the REST search rather than the MCP query_brain tool: an SSE
// handshake does not fit an 800ms budget, and query_brain's miss path escalates
// to a 15-second live extraction that a hook must never wait on. The caller
// supplies the deadline through ctx.
func (c *Client) SearchSkills(ctx context.Context, q string, limit int) ([]SkillHit, error) {
	u := fmt.Sprintf("%s/skills/search?q=%s&limit=%d", c.BaseURL, url.QueryEscape(q), limit)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-API-Key", c.APIKey)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET /skills/search: %s", resp.Status)
	}
	var env envelope
	var hits []SkillHit
	if err := json.Unmarshal(raw, &env); err == nil && len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &hits); err != nil {
			return nil, err
		}
		return hits, nil
	}
	if err := json.Unmarshal(raw, &hits); err != nil {
		return nil, err
	}
	return hits, nil
}

func snippet(b []byte) string {
	s := strings.TrimSpace(string(b))
	if len(s) > 300 {
		return s[:300]
	}
	return s
}
