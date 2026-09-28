package namecheap

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
)

func TestPresentPreservesExistingRecords(t *testing.T) {
	t.Parallel()

	var (
		mu               sync.Mutex
		postForm         url.Values
		challengePresent bool
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			mu.Lock()
			present := challengePresent
			mu.Unlock()
			challenge := ""
			if present {
				challenge = `<host Name="_acme-challenge.home" Type="TXT" Address="validation" MXPref="10" TTL="120"/>`
			}
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSGetHostsResult>`+
				`<host Name="@" Type="A" Address="192.0.2.1" MXPref="10" TTL="300"/>`+
				`<host Name="_acme-challenge.home" Type="TXT" Address="other-validation" MXPref="10" TTL="120"/>`+
				challenge+`</DomainDNSGetHostsResult></CommandResponse></ApiResponse>`)
		case http.MethodPost:
			if err := r.ParseForm(); err != nil {
				t.Error(err)
			}
			mu.Lock()
			postForm = make(url.Values, len(r.PostForm))
			for key, values := range r.PostForm {
				postForm[key] = append([]string(nil), values...)
			}
			challengePresent = true
			mu.Unlock()
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSSetHostsResult IsSuccess="true"/></CommandResponse></ApiResponse>`)
		default:
			http.Error(w, "unsupported", http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	client, err := New(Options{
		Username: "user",
		Token:    "token",
		ClientIP: "198.51.100.10",
		TTL:      120,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	client.baseURL = server.URL

	if err := client.Present(context.Background(), "_acme-challenge.home.example.co.uk.", "validation"); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if postForm.Get("SLD") != "example" || postForm.Get("TLD") != "co.uk" {
		t.Fatalf("domain split: SLD=%q TLD=%q", postForm.Get("SLD"), postForm.Get("TLD"))
	}
	if postForm.Get("HostName1") != "@" || postForm.Get("Address1") != "192.0.2.1" {
		t.Fatalf("existing record not preserved: %#v", postForm)
	}
	if postForm.Get("HostName2") != "_acme-challenge.home" || postForm.Get("Address2") != "other-validation" {
		t.Fatalf("existing challenge record not preserved: %#v", postForm)
	}
	if postForm.Get("HostName3") != "_acme-challenge.home" || postForm.Get("Address3") != "validation" {
		t.Fatalf("challenge record missing: %#v", postForm)
	}
	if postForm.Get("ClientIp") != "198.51.100.10" {
		t.Fatalf("client IP = %q", postForm.Get("ClientIp"))
	}
}

func TestCleanupRemovesOnlyMatchingChallenge(t *testing.T) {
	t.Parallel()

	var (
		mu       sync.Mutex
		postBody string
		cleaned  bool
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			mu.Lock()
			wasCleaned := cleaned
			mu.Unlock()
			remove := `<host Name="_acme-challenge" Type="TXT" Address="remove-me" MXPref="10" TTL="120"/>`
			if wasCleaned {
				remove = ""
			}
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSGetHostsResult>`+
				remove+`<host Name="_acme-challenge" Type="TXT" Address="keep-me" MXPref="10" TTL="120"/>`+
				`</DomainDNSGetHostsResult></CommandResponse></ApiResponse>`)
		case http.MethodPost:
			raw, _ := io.ReadAll(r.Body)
			mu.Lock()
			postBody = string(raw)
			cleaned = true
			mu.Unlock()
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSSetHostsResult IsSuccess="true"/></CommandResponse></ApiResponse>`)
		}
	}))
	defer server.Close()

	client, err := New(Options{
		Username: "user",
		Token:    "token",
		ClientIP: "198.51.100.10",
		TTL:      120,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	client.baseURL = server.URL

	if err := client.Cleanup(context.Background(), "_acme-challenge.example.com", "remove-me"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if strings.Contains(postBody, "remove-me") || !strings.Contains(postBody, "keep-me") {
		t.Fatalf("unexpected setHosts body: %s", postBody)
	}
}

func TestPresentRetriesTransientHTTPFailure(t *testing.T) {
	t.Parallel()

	var (
		mu       sync.Mutex
		requests int
		present  bool
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		requests++
		requestNumber := requests
		isPresent := present
		mu.Unlock()
		if requestNumber == 1 {
			http.Error(w, "temporary failure", http.StatusBadGateway)
			return
		}
		if r.Method == http.MethodGet {
			challenge := ""
			if isPresent {
				challenge = `<host Name="_acme-challenge" Type="TXT" Address="validation" MXPref="10" TTL="120"/>`
			}
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSGetHostsResult>`+challenge+`</DomainDNSGetHostsResult></CommandResponse></ApiResponse>`)
			return
		}
		mu.Lock()
		present = true
		mu.Unlock()
		io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSSetHostsResult IsSuccess="true"/></CommandResponse></ApiResponse>`)
	}))
	defer server.Close()

	client, err := New(Options{
		Username: "user",
		Token:    "token",
		ClientIP: "198.51.100.10",
		TTL:      120,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	client.baseURL = server.URL

	if err := client.Present(context.Background(), "_acme-challenge.example.com", "validation"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if requests != 4 {
		t.Fatalf("requests = %d", requests)
	}
}

func TestPresentRepairsOverwrittenZoneUpdate(t *testing.T) {
	t.Parallel()

	var (
		mu          sync.Mutex
		setAttempts int
		present     bool
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			mu.Lock()
			isPresent := present
			mu.Unlock()
			challenge := ""
			if isPresent {
				challenge = `<host Name="_acme-challenge" Type="TXT" Address="validation" MXPref="10" TTL="120"/>`
			}
			io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSGetHostsResult>`+challenge+`</DomainDNSGetHostsResult></CommandResponse></ApiResponse>`)
			return
		}

		mu.Lock()
		setAttempts++
		if setAttempts >= 2 {
			present = true
		}
		mu.Unlock()
		io.WriteString(w, `<ApiResponse Status="OK"><Errors/><CommandResponse><DomainDNSSetHostsResult IsSuccess="true"/></CommandResponse></ApiResponse>`)
	}))
	defer server.Close()

	client, err := New(Options{
		Username: "user",
		Token:    "token",
		ClientIP: "198.51.100.10",
		TTL:      120,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	client.baseURL = server.URL

	if err := client.Present(context.Background(), "_acme-challenge.example.com", "validation"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if setAttempts != 2 {
		t.Fatalf("set attempts = %d", setAttempts)
	}
}
