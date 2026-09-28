package namecheap

import (
	"context"
	"crypto/rand"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/publicsuffix"
)

const (
	defaultBaseURL   = "https://api.namecheap.com/xml.response"
	defaultGetIPURL  = "https://dynamicdns.park-your-domain.com/getip"
	mutationAttempts = 6
)

type Options struct {
	Username string
	Token    string
	ClientIP string
	TTL      int
}

type Client struct {
	options  Options
	baseURL  string
	getIPURL string
	http     *http.Client
	mu       sync.Mutex
}

type Record struct {
	Type    string `xml:",attr"`
	Name    string `xml:",attr"`
	Address string `xml:",attr"`
	MXPref  string `xml:",attr"`
	TTL     string `xml:",attr"`
}

type apiError struct {
	Number      int    `xml:",attr"`
	Description string `xml:",chardata"`
}

func (e apiError) Error() string {
	return fmt.Sprintf("%s [%d]", strings.TrimSpace(e.Description), e.Number)
}

type getHostsResponse struct {
	XMLName xml.Name   `xml:"ApiResponse"`
	Status  string     `xml:"Status,attr"`
	Errors  []apiError `xml:"Errors>Error"`
	Hosts   []Record   `xml:"CommandResponse>DomainDNSGetHostsResult>host"`
}

type setHostsResponse struct {
	XMLName xml.Name   `xml:"ApiResponse"`
	Status  string     `xml:"Status,attr"`
	Errors  []apiError `xml:"Errors>Error"`
	Result  struct {
		IsSuccess string `xml:",attr"`
	} `xml:"CommandResponse>DomainDNSSetHostsResult"`
}

func New(options Options, proxy string) (*Client, error) {
	if options.Username == "" || options.Token == "" {
		return nil, errors.New("Namecheap credentials are required")
	}
	if options.TTL <= 0 {
		return nil, errors.New("Namecheap TTL must be positive")
	}

	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("default HTTP transport is not configurable")
	}
	transport = transport.Clone()

	if proxy != "" {
		proxyURL, err := url.Parse(proxy)
		if err != nil {
			return nil, fmt.Errorf("parse Namecheap proxy: %w", err)
		}
		if proxyURL.Scheme == "" || proxyURL.Host == "" {
			return nil, errors.New("Namecheap proxy must include scheme and host")
		}
		transport.Proxy = http.ProxyURL(proxyURL)
	} else {
		transport.Proxy = nil
	}

	return &Client{
		options:  options,
		baseURL:  defaultBaseURL,
		getIPURL: defaultGetIPURL,
		http: &http.Client{
			Timeout:   time.Minute,
			Transport: transport,
		},
	}, nil
}

func (c *Client) Present(ctx context.Context, fqdn, value string) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	sld, tld, host, err := splitRecord(fqdn)
	if err != nil {
		return err
	}

	desired := Record{
		Type:    "TXT",
		Name:    host,
		Address: value,
		MXPref:  "10",
		TTL:     strconv.Itoa(c.options.TTL),
	}
	var lastErr error

	for attempt := 1; attempt <= mutationAttempts; attempt++ {
		records, err := c.getHosts(ctx, sld, tld)
		if err != nil {
			lastErr = err
		} else if containsRecord(records, desired) {
			return nil
		} else {
			records = append(records, desired)
			if err := c.setHosts(ctx, sld, tld, records); err != nil {
				lastErr = err
			} else if !waitForMutation(ctx, attempt) {
				return ctx.Err()
			} else {
				current, err := c.getHosts(ctx, sld, tld)
				if err == nil && containsRecord(current, desired) {
					return nil
				}
				if err != nil {
					lastErr = err
				} else {
					lastErr = errors.New("challenge record was overwritten by a concurrent zone update")
				}
			}
		}

		if attempt < mutationAttempts && !waitForMutation(ctx, attempt) {
			return ctx.Err()
		}
	}

	return fmt.Errorf("publish Namecheap challenge after %d attempts: %w", mutationAttempts, lastErr)
}

func (c *Client) Cleanup(ctx context.Context, fqdn, value string) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	sld, tld, host, err := splitRecord(fqdn)
	if err != nil {
		return err
	}

	target := Record{Type: "TXT", Name: host, Address: value}
	var lastErr error

	for attempt := 1; attempt <= mutationAttempts; attempt++ {
		records, err := c.getHosts(ctx, sld, tld)
		if err != nil {
			lastErr = err
		} else if !containsRecord(records, target) {
			return nil
		} else {
			filtered := make([]Record, 0, len(records)-1)
			for _, record := range records {
				if recordMatches(record, target) {
					continue
				}
				filtered = append(filtered, record)
			}

			if err := c.setHosts(ctx, sld, tld, filtered); err != nil {
				lastErr = err
			} else if !waitForMutation(ctx, attempt) {
				return ctx.Err()
			} else {
				current, err := c.getHosts(ctx, sld, tld)
				if err == nil && !containsRecord(current, target) {
					return nil
				}
				if err != nil {
					lastErr = err
				} else {
					lastErr = errors.New("challenge cleanup was overwritten by a concurrent zone update")
				}
			}
		}

		if attempt < mutationAttempts && !waitForMutation(ctx, attempt) {
			return ctx.Err()
		}
	}

	return fmt.Errorf("remove Namecheap challenge after %d attempts: %w", mutationAttempts, lastErr)
}

func (c *Client) getHosts(ctx context.Context, sld, tld string) ([]Record, error) {
	values, err := c.query(ctx, "namecheap.domains.dns.getHosts")
	if err != nil {
		return nil, err
	}
	values.Set("SLD", sld)
	values.Set("TLD", tld)

	endpoint, err := url.Parse(c.baseURL)
	if err != nil {
		return nil, fmt.Errorf("parse Namecheap endpoint: %w", err)
	}
	endpoint.RawQuery = values.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("create Namecheap getHosts request: %w", err)
	}

	var response getHostsResponse
	if err := c.do(req, &response); err != nil {
		return nil, fmt.Errorf("Namecheap getHosts failed: %w", err)
	}
	if len(response.Errors) > 0 {
		return nil, response.Errors[0]
	}

	return response.Hosts, nil
}

func (c *Client) setHosts(ctx context.Context, sld, tld string, records []Record) error {
	values, err := c.query(ctx, "namecheap.domains.dns.setHosts")
	if err != nil {
		return err
	}
	values.Set("SLD", sld)
	values.Set("TLD", tld)

	for i, record := range records {
		index := strconv.Itoa(i + 1)
		values.Set("HostName"+index, record.Name)
		values.Set("RecordType"+index, record.Type)
		values.Set("Address"+index, record.Address)
		values.Set("MXPref"+index, record.MXPref)
		values.Set("TTL"+index, record.TTL)
	}

	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.baseURL,
		strings.NewReader(values.Encode()),
	)
	if err != nil {
		return fmt.Errorf("create Namecheap setHosts request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	var response setHostsResponse
	if err := c.do(req, &response); err != nil {
		return fmt.Errorf("Namecheap setHosts failed: %w", err)
	}
	if len(response.Errors) > 0 {
		return response.Errors[0]
	}
	if response.Result.IsSuccess != "true" {
		return errors.New("Namecheap setHosts did not report success")
	}

	return nil
}

func (c *Client) query(ctx context.Context, command string) (url.Values, error) {
	clientIP := c.options.ClientIP
	if clientIP == "" {
		var err error
		clientIP, err = c.discoverClientIP(ctx)
		if err != nil {
			return nil, err
		}
	}

	values := make(url.Values)
	values.Set("ApiUser", c.options.Username)
	values.Set("ApiKey", c.options.Token)
	values.Set("UserName", c.options.Username)
	values.Set("Command", command)
	values.Set("ClientIp", clientIP)
	return values, nil
}

func (c *Client) discoverClientIP(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.getIPURL, nil)
	if err != nil {
		return "", fmt.Errorf("create Namecheap IP discovery request: %w", err)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return "", errors.New("Namecheap IP discovery request failed")
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		return "", fmt.Errorf("Namecheap IP discovery returned HTTP %d", resp.StatusCode)
	}

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1024))
	if err != nil {
		return "", fmt.Errorf("read Namecheap IP discovery response: %w", err)
	}
	clientIP := strings.TrimSpace(string(raw))
	if clientIP == "" {
		return "", errors.New("Namecheap IP discovery returned an empty address")
	}

	c.options.ClientIP = clientIP
	return clientIP, nil
}

func (c *Client) do(req *http.Request, result any) error {
	req.Header.Set("User-Agent", "homehub-acme-courier/1")

	for attempt := 1; attempt <= 3; attempt++ {
		attemptRequest := req.Clone(req.Context())
		if req.GetBody != nil {
			body, err := req.GetBody()
			if err != nil {
				return errors.New("recreate Namecheap API request body")
			}
			attemptRequest.Body = body
		}

		resp, err := c.http.Do(attemptRequest)
		if err != nil {
			if attempt < 3 {
				if waitBeforeRetry(req.Context(), attempt) {
					continue
				}
				if contextErr := req.Context().Err(); contextErr != nil {
					return contextErr
				}
			}
			return errors.New("Namecheap API request failed")
		}

		retryable := resp.StatusCode == http.StatusTooManyRequests ||
			resp.StatusCode >= http.StatusInternalServerError
		if retryable && attempt < 3 {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
			resp.Body.Close()
			if waitBeforeRetry(req.Context(), attempt) {
				continue
			}
			return req.Context().Err()
		}

		if resp.StatusCode >= http.StatusBadRequest {
			resp.Body.Close()
			return fmt.Errorf("Namecheap API returned HTTP %d", resp.StatusCode)
		}

		decoder := xml.NewDecoder(io.LimitReader(resp.Body, 4<<20))
		err = decoder.Decode(result)
		resp.Body.Close()
		if err != nil {
			return fmt.Errorf("decode Namecheap API response: %w", err)
		}
		return nil
	}

	return errors.New("Namecheap API request failed after retries")
}

func waitBeforeRetry(ctx context.Context, attempt int) bool {
	timer := time.NewTimer(time.Duration(attempt) * time.Second)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func containsRecord(records []Record, expected Record) bool {
	for _, record := range records {
		if recordMatches(record, expected) {
			return true
		}
	}
	return false
}

func recordMatches(record, expected Record) bool {
	return strings.EqualFold(record.Type, expected.Type) &&
		record.Name == expected.Name &&
		record.Address == expected.Address
}

func waitForMutation(ctx context.Context, attempt int) bool {
	jitter, err := rand.Int(rand.Reader, big.NewInt(int64(500*time.Millisecond)))
	if err != nil {
		jitter = big.NewInt(0)
	}
	delay := time.Duration(attempt)*250*time.Millisecond + time.Duration(jitter.Int64())
	timer := time.NewTimer(delay)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func splitRecord(fqdn string) (sld, tld, host string, err error) {
	fqdn = strings.TrimSuffix(strings.ToLower(strings.TrimSpace(fqdn)), ".")
	root, err := publicsuffix.EffectiveTLDPlusOne(fqdn)
	if err != nil {
		return "", "", "", fmt.Errorf("determine Namecheap root domain for %q: %w", fqdn, err)
	}
	tld, _ = publicsuffix.PublicSuffix(root)
	sld = strings.TrimSuffix(root, "."+tld)

	if fqdn == root {
		host = "@"
	} else {
		host = strings.TrimSuffix(fqdn, "."+root)
	}
	if sld == "" || tld == "" || host == "" {
		return "", "", "", fmt.Errorf("invalid Namecheap record name %q", fqdn)
	}
	return sld, tld, host, nil
}
