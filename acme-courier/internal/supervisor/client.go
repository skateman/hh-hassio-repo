package supervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var appSlugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)

type Client struct {
	baseURL string
	token   string
	http    *http.Client
}

type response struct {
	Result  string `json:"result"`
	Message string `json:"message"`
}

func New(baseURL, token string) *Client {
	return &Client{
		baseURL: strings.TrimSuffix(baseURL, "/"),
		token:   token,
		http:    &http.Client{Timeout: 5 * time.Minute},
	}
}

func (c *Client) Restart(ctx context.Context, slug string) error {
	if !appSlugPattern.MatchString(slug) {
		return fmt.Errorf("invalid Home Assistant app slug %q", slug)
	}
	if c.token == "" {
		return errors.New("SUPERVISOR_TOKEN is unavailable")
	}

	endpoint := c.baseURL + "/addons/" + url.PathEscape(slug) + "/restart"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(nil))
	if err != nil {
		return fmt.Errorf("create Supervisor restart request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")

	httpResponse, err := c.http.Do(request)
	if err != nil {
		return fmt.Errorf("restart Home Assistant app %s: %w", slug, err)
	}
	defer httpResponse.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(httpResponse.Body, 64<<10))
	if err != nil {
		return fmt.Errorf("read Supervisor restart response for %s: %w", slug, err)
	}

	var result response
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &result); err != nil {
			return fmt.Errorf("decode Supervisor restart response for %s: %w", slug, err)
		}
	}
	if httpResponse.StatusCode >= http.StatusBadRequest || result.Result != "ok" {
		message := result.Message
		if message == "" {
			message = http.StatusText(httpResponse.StatusCode)
		}
		return fmt.Errorf("Supervisor refused restart of %s: %s", slug, message)
	}

	return nil
}
