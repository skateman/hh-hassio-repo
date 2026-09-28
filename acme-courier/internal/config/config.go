package config

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/robfig/cron/v3"
	"gopkg.in/yaml.v3"
)

const (
	DefaultCronRenew       = "12 1 * * *"
	DefaultRenewBeforeDays = 30
	DefaultTTL             = 120
	DefaultRenewJitter     = 30 * time.Minute
)

var (
	allowedFiles = map[string]struct{}{
		"cert.pem":      {},
		"chain.pem":     {},
		"fullchain.pem": {},
		"privkey.pem":   {},
	}
	cronParser = cron.NewParser(cron.Minute | cron.Hour | cron.Dom | cron.Month | cron.Dow)
)

type Config struct {
	Draft        bool          `yaml:"draft,omitempty"`
	ACME         ACMEConfig    `yaml:"acme"`
	Profiles     []Profile     `yaml:"profiles"`
	Certificates []Certificate `yaml:"certificates"`
}

type ACMEConfig struct {
	EmailAccount    string   `yaml:"email_account"`
	CronRenew       string   `yaml:"crontab_renew,omitempty"`
	DirectoryURL    string   `yaml:"directory_url,omitempty"`
	Staging         bool     `yaml:"staging,omitempty"`
	RenewBeforeDays int      `yaml:"renew_before_days,omitempty"`
	RenewJitter     Duration `yaml:"renew_jitter,omitempty"`
}

type Profile struct {
	Name            string          `yaml:"name"`
	Provider        string          `yaml:"provider"`
	ProviderOptions ProviderOptions `yaml:"provider_options"`
}

type ProviderOptions struct {
	AuthUsername       string   `yaml:"auth_username"`
	AuthToken          string   `yaml:"auth_token"`
	AuthClientIP       string   `yaml:"auth_client_ip,omitempty"`
	TTL                int      `yaml:"ttl,omitempty"`
	PropagationTimeout Duration `yaml:"propagation_timeout,omitempty"`
	PollingInterval    Duration `yaml:"polling_interval,omitempty"`
}

type Certificate struct {
	Name       string         `yaml:"name,omitempty"`
	Domains    []string       `yaml:"domains"`
	Profile    string         `yaml:"profile"`
	Deploy     []DeployTarget `yaml:"deploy,omitempty"`
	ForceRenew bool           `yaml:"force_renew,omitempty"`
	ReuseKey   bool           `yaml:"reuse_key,omitempty"`
	KeyType    string         `yaml:"key_type,omitempty"`
}

type DeployTarget struct {
	Path           string   `yaml:"path"`
	IdentityFile   string   `yaml:"identity_file,omitempty"`
	KnownHostsFile string   `yaml:"known_hosts_file,omitempty"`
	Port           int      `yaml:"port,omitempty"`
	Files          []string `yaml:"files,omitempty"`
}

type Duration struct {
	time.Duration
	set bool
}

func (d *Duration) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind != yaml.ScalarNode {
		return errors.New("duration must be a scalar")
	}

	if seconds, err := strconv.Atoi(node.Value); err == nil {
		d.Duration = time.Duration(seconds) * time.Second
		d.set = true
		return nil
	}

	value, err := time.ParseDuration(node.Value)
	if err != nil {
		return fmt.Errorf("invalid duration %q: %w", node.Value, err)
	}

	d.Duration = value
	d.set = true
	return nil
}

func Load(path string) (*Config, []byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, fmt.Errorf("read configuration: %w", err)
	}

	decoder := yaml.NewDecoder(bytes.NewReader(raw))
	decoder.KnownFields(true)

	var cfg Config
	if err := decoder.Decode(&cfg); err != nil {
		return nil, nil, fmt.Errorf("parse configuration: %w", err)
	}

	cfg.applyDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, nil, err
	}

	return &cfg, raw, nil
}

func (c *Config) applyDefaults() {
	if c.ACME.CronRenew == "" {
		c.ACME.CronRenew = DefaultCronRenew
	}
	if c.ACME.RenewBeforeDays == 0 {
		c.ACME.RenewBeforeDays = DefaultRenewBeforeDays
	}
	if !c.ACME.RenewJitter.set {
		c.ACME.RenewJitter.Duration = DefaultRenewJitter
	}

	for i := range c.Profiles {
		options := &c.Profiles[i].ProviderOptions
		if options.TTL == 0 {
			options.TTL = DefaultTTL
		}
		if !options.PropagationTimeout.set {
			options.PropagationTimeout.Duration = time.Hour
		}
		if !options.PollingInterval.set {
			options.PollingInterval.Duration = 15 * time.Second
		}
	}

	for i := range c.Certificates {
		certificate := &c.Certificates[i]
		if certificate.Name == "" && len(certificate.Domains) > 0 {
			certificate.Name = strings.TrimPrefix(certificate.Domains[0], "*.")
		}
		if certificate.KeyType == "" {
			certificate.KeyType = "rsa"
		}
		for j := range certificate.Deploy {
			if certificate.Deploy[j].Port == 0 && certificate.Deploy[j].IsRemote() {
				certificate.Deploy[j].Port = 22
			}
		}
	}
}

func (c *Config) Validate() error {
	if c.ACME.EmailAccount == "" {
		return errors.New("acme.email_account is required")
	}
	if c.ACME.RenewBeforeDays < 1 {
		return errors.New("acme.renew_before_days must be positive")
	}
	if c.ACME.RenewJitter.Duration < 0 {
		return errors.New("acme.renew_jitter must not be negative")
	}
	if _, err := cronParser.Parse(c.ACME.CronRenew); err != nil {
		return fmt.Errorf("invalid acme.crontab_renew: %w", err)
	}

	profiles := make(map[string]Profile, len(c.Profiles))
	for i, profile := range c.Profiles {
		prefix := fmt.Sprintf("profiles[%d]", i)
		if profile.Name == "" {
			return fmt.Errorf("%s.name is required", prefix)
		}
		if _, exists := profiles[profile.Name]; exists {
			return fmt.Errorf("profile %q is duplicated", profile.Name)
		}
		if profile.Provider != "namecheap" {
			return fmt.Errorf("%s.provider must be namecheap", prefix)
		}
		if profile.ProviderOptions.AuthUsername == "" {
			return fmt.Errorf("%s.provider_options.auth_username is required", prefix)
		}
		if profile.ProviderOptions.AuthToken == "" {
			return fmt.Errorf("%s.provider_options.auth_token is required", prefix)
		}
		if profile.ProviderOptions.TTL < 1 {
			return fmt.Errorf("%s.provider_options.ttl must be positive", prefix)
		}
		if profile.ProviderOptions.PropagationTimeout.Duration <= 0 {
			return fmt.Errorf("%s.provider_options.propagation_timeout must be positive", prefix)
		}
		if profile.ProviderOptions.PollingInterval.Duration <= 0 {
			return fmt.Errorf("%s.provider_options.polling_interval must be positive", prefix)
		}
		profiles[profile.Name] = profile
	}

	if len(c.Certificates) == 0 && !c.Draft {
		return errors.New("at least one certificate is required")
	}

	names := make(map[string]struct{}, len(c.Certificates))
	for i, certificate := range c.Certificates {
		prefix := fmt.Sprintf("certificates[%d]", i)
		if certificate.Name == "" {
			return fmt.Errorf("%s.name is required", prefix)
		}
		if certificate.Name != filepath.Base(certificate.Name) || certificate.Name == "." {
			return fmt.Errorf("%s.name must be a safe path segment", prefix)
		}
		if _, exists := names[certificate.Name]; exists {
			return fmt.Errorf("certificate %q is duplicated", certificate.Name)
		}
		names[certificate.Name] = struct{}{}

		if len(certificate.Domains) == 0 {
			return fmt.Errorf("%s.domains must not be empty", prefix)
		}
		for j, domain := range certificate.Domains {
			if strings.TrimSpace(domain) == "" || !strings.Contains(domain, ".") {
				return fmt.Errorf("%s.domains[%d] is invalid", prefix, j)
			}
		}
		if _, exists := profiles[certificate.Profile]; !exists {
			return fmt.Errorf("%s.profile %q does not exist", prefix, certificate.Profile)
		}
		if certificate.KeyType != "rsa" && certificate.KeyType != "ecdsa" {
			return fmt.Errorf("%s.key_type must be rsa or ecdsa", prefix)
		}

		for j, target := range certificate.Deploy {
			if err := target.Validate(); err != nil {
				return fmt.Errorf("%s.deploy[%d]: %w", prefix, j, err)
			}
		}
	}

	return nil
}

func (c *Config) Profile(name string) (Profile, bool) {
	for _, profile := range c.Profiles {
		if profile.Name == name {
			return profile, true
		}
	}
	return Profile{}, false
}

func (c ACMEConfig) ServerURL() string {
	if c.DirectoryURL != "" {
		return c.DirectoryURL
	}
	if c.Staging {
		return "https://acme-staging-v02.api.letsencrypt.org/directory"
	}
	return "https://acme-v02.api.letsencrypt.org/directory"
}

func (d DeployTarget) IsRemote() bool {
	at := strings.IndexByte(d.Path, '@')
	if at < 1 {
		return false
	}
	return strings.IndexByte(d.Path[at+1:], ':') > 0
}

func (d DeployTarget) SelectedFiles() []string {
	if len(d.Files) == 0 {
		return []string{"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"}
	}
	return append([]string(nil), d.Files...)
}

func (d DeployTarget) Validate() error {
	if d.Path == "" {
		return errors.New("path is required")
	}

	if d.IsRemote() {
		if d.IdentityFile == "" {
			return errors.New("identity_file is required for an SSH target")
		}
		if !filepath.IsAbs(d.IdentityFile) {
			return errors.New("identity_file must be absolute")
		}
		if d.KnownHostsFile != "" && !filepath.IsAbs(d.KnownHostsFile) {
			return errors.New("known_hosts_file must be absolute")
		}
		if d.Port < 1 || d.Port > 65535 {
			return errors.New("port must be between 1 and 65535")
		}
	} else {
		if !filepath.IsAbs(d.Path) {
			return errors.New("local path must be absolute")
		}
		if d.IdentityFile != "" || d.KnownHostsFile != "" || d.Port != 0 {
			return errors.New("SSH options are only valid for a remote target")
		}
	}

	seen := make(map[string]struct{}, len(d.Files))
	for _, name := range d.Files {
		if _, allowed := allowedFiles[name]; !allowed {
			return fmt.Errorf("unsupported file %q", name)
		}
		if _, duplicate := seen[name]; duplicate {
			return fmt.Errorf("file %q is duplicated", name)
		}
		seen[name] = struct{}{}
	}

	return nil
}
