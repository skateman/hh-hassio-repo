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
profiles:
  - name: namecheap
    provider: namecheap
    provider_options:
      auth_username: user
      auth_token: token
      auth_client_ip: 127.0.0.1
certificates:
  - name: home.example.com
    domains:
      - home.example.com
      - '*.home.example.com'
    profile: namecheap
    deploy:
      - path: /ssl
      - path: homeassistant@10.0.0.2:/etc/ssl/homeassistant/
        identity_file: /config/.ssh/id_ed25519
        known_hosts_file: /config/.ssh/known_hosts
        files:
          - fullchain.pem
          - privkey.pem
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
	if got := cfg.Profiles[0].ProviderOptions.PropagationTimeout.Duration; got != time.Hour {
		t.Fatalf("propagation timeout = %s", got)
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
}

func TestLoadAllowsDisablingRenewalJitter(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "config.yml")
	content := `
acme:
  email_account: admin@example.com
  renew_jitter: 0s
profiles:
  - name: namecheap
    provider: namecheap
    provider_options:
      auth_username: user
      auth_token: token
certificates:
  - name: home.example.com
    domains: [home.example.com]
    profile: namecheap
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
profiles:
  - name: namecheap
    provider: namecheap
    provider_options:
      auth_username: user
      auth_token: token
certificates:
  - name: home.example.com
    domains: [home.example.com]
    profile: namecheap
    deploy_hook: echo unsafe
`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, _, err := Load(path); err == nil {
		t.Fatal("expected deploy_hook to be rejected")
	}
}
