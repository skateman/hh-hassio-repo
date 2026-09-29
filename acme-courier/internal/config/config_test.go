package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoadAppliesDefaultsAndParsesDeployTargets(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "config.yml")
	content := `
acme:
  email_account: admin@example.com
  crontab_renew: 12 01 * * *
namecheap:
  auth_username: user
  auth_token: token
  auth_client_ip: 127.0.0.1
  proxy: http://proxy.example.com:8080
certificates:
  - name: home.example.com
    domains:
      - home.example.com
      - '*.home.example.com'
    deploy:
      - path: /ssl
      - path: homeassistant@10.0.0.2:/etc/ssl/homeassistant/
        identity_file: /config/.ssh/id_ed25519
        known_hosts_file: /config/.ssh/known_hosts
        files:
          - fullchain.pem
          - privkey.pem
    restart_apps:
      - core_nginx_proxy
`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, _, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}

	if cfg.ACME.RenewBeforeDays != DefaultRenewBeforeDays {
		t.Fatalf("renew_before_days = %d", cfg.ACME.RenewBeforeDays)
	}
	if cfg.ACME.RenewJitter.Duration != DefaultRenewJitter {
		t.Fatalf("renew_jitter = %s", cfg.ACME.RenewJitter.Duration)
	}
	if got := cfg.Namecheap.PropagationTimeout.Duration; got != time.Hour {
		t.Fatalf("propagation timeout = %s", got)
	}
	if cfg.Namecheap.Proxy != "http://proxy.example.com:8080" {
		t.Fatalf("proxy = %q", cfg.Namecheap.Proxy)
	}
	if cfg.Certificates[0].KeyType != "rsa" {
		t.Fatalf("key type = %q", cfg.Certificates[0].KeyType)
	}
	if cfg.Certificates[0].Deploy[0].IsRemote() {
		t.Fatal("local target detected as remote")
	}
	if !cfg.Certificates[0].Deploy[1].IsRemote() {
		t.Fatal("SSH target not detected as remote")
	}
	if cfg.Certificates[0].Deploy[1].Port != 22 {
		t.Fatalf("SSH port = %d", cfg.Certificates[0].Deploy[1].Port)
	}
	if got := cfg.Certificates[0].Deploy[0].SelectedFiles(); len(got) != 4 {
		t.Fatalf("default files = %#v", got)
	}
	if got := cfg.Certificates[0].RestartApps; len(got) != 1 || got[0] != "core_nginx_proxy" {
		t.Fatalf("restart apps = %#v", got)
	}
}

func TestLoadAllowsDisablingRenewalJitter(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "config.yml")
	content := `
acme:
  email_account: admin@example.com
  renew_jitter: 0s
namecheap:
  auth_username: user
  auth_token: token
certificates:
  - name: home.example.com
    domains: [home.example.com]
`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, _, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ACME.RenewJitter.Duration != 0 {
		t.Fatalf("renew_jitter = %s", cfg.ACME.RenewJitter.Duration)
	}
}

func TestLoadRejectsLegacyDeployHook(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "config.yml")
	content := `
acme:
  email_account: admin@example.com
namecheap:
  auth_username: user
  auth_token: token
certificates:
  - name: home.example.com
    domains: [home.example.com]
    deploy_hook: echo unsafe
`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, _, err := Load(path); err == nil {
		t.Fatal("expected deploy_hook to be rejected")
	}
}

func TestLoadHomeAssistantOptionsJSON(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "options.json")
	content := `{
  "draft": false,
  "acme": {
    "email_account": "admin@example.com",
    "crontab_renew": "12 01 * * *",
    "directory_url": "",
    "staging": true,
    "renew_before_days": 30,
    "renew_jitter": "2h"
  },
  "namecheap": {
    "auth_username": "user",
    "auth_token": "token",
    "auth_client_ip": "127.0.0.1",
    "proxy": "http://proxy.example.com:8080",
    "ttl": 120,
    "propagation_timeout": "1h",
    "polling_interval": "15s"
  },
  "certificates": [{
    "name": "home.example.com",
    "domains": ["home.example.com"],
    "deploy": [{"path": "/ssl"}]
  }]
}`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, _, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.ACME.Staging || cfg.ACME.RenewJitter.Duration != 2*time.Hour {
		t.Fatalf("acme config = %#v", cfg.ACME)
	}
	if cfg.Namecheap.Proxy != "http://proxy.example.com:8080" {
		t.Fatalf("proxy = %q", cfg.Namecheap.Proxy)
	}
}

func TestLoadRejectsRestartingSelf(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "config.yml")
	content := `
acme:
  email_account: admin@example.com
namecheap:
  auth_username: user
  auth_token: token
certificates:
  - name: home.example.com
    domains: [home.example.com]
    restart_apps:
      - 5b84fcb2_acme-courier
`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, _, err := Load(path); err == nil {
		t.Fatal("expected self restart to be rejected")
	}
}
