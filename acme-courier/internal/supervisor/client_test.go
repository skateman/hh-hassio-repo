package supervisor

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRestart(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/addons/core_nginx_proxy/restart" {
			t.Fatalf("path = %q", request.URL.Path)
		}
		if request.Header.Get("Authorization") != "Bearer token" {
			t.Fatalf("authorization = %q", request.Header.Get("Authorization"))
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"result":"ok","data":{}}`)
	}))
	defer server.Close()

	client := New(server.URL, "token")
	if err := client.Restart(context.Background(), "core_nginx_proxy"); err != nil {
		t.Fatal(err)
	}
}

func TestRestartRequiresToken(t *testing.T) {
	t.Parallel()

	client := New("http://supervisor", "")
	if err := client.Restart(context.Background(), "core_nginx_proxy"); err == nil {
		t.Fatal("expected missing token error")
	}
}
