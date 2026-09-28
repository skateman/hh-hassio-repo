package certmanager

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"hash"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/acme"

	"github.com/skateman/hh-hassio-repo/acme-courier/internal/config"
	"github.com/skateman/hh-hassio-repo/acme-courier/internal/deploy"
	"github.com/skateman/hh-hassio-repo/acme-courier/internal/namecheap"
)

type Manager struct {
	storage  string
	proxy    string
	deployer *deploy.Deployer
	logger   *slog.Logger
	now      func() time.Time
}

type challengeRecord struct {
	name          string
	value         string
	authorization string
	challenge     *acme.Challenge
}

func New(storage, proxy string, deployer *deploy.Deployer, logger *slog.Logger) *Manager {
	return &Manager{
		storage:  storage,
		proxy:    proxy,
		deployer: deployer,
		logger:   logger,
		now:      time.Now,
	}
}

func (m *Manager) Reconcile(ctx context.Context, cfg *config.Config, reason string) error {
	if cfg.Draft {
		m.logger.Info("configuration is in draft mode; skipping reconciliation")
		return nil
	}

	if err := os.MkdirAll(filepath.Join(m.storage, "live"), 0o750); err != nil {
		return fmt.Errorf("create certificate storage: %w", err)
	}

	var failures []error
	for _, certificate := range cfg.Certificates {
		if err := m.reconcileCertificate(ctx, cfg, certificate, reason); err != nil {
			failures = append(failures, fmt.Errorf("certificate %q: %w", certificate.Name, err))
		}
	}
	return errors.Join(failures...)
}

func (m *Manager) reconcileCertificate(
	ctx context.Context,
	cfg *config.Config,
	certificate config.Certificate,
	reason string,
) error {
	m.logger.Info("reconciling certificate", "name", certificate.Name, "reason", reason)

	files, leaf, loadErr := m.loadCertificate(certificate.Name)
	renew := certificate.ForceRenew || loadErr != nil
	if loadErr == nil {
		renew = renew || shouldRenew(leaf, certificate.Domains, cfg.ACME.RenewBeforeDays, m.now())
	}

	if renew {
		if reason == "schedule" && cfg.ACME.RenewJitter.Duration > 0 {
			delay, err := randomDelay(cfg.ACME.RenewJitter.Duration)
			if err != nil {
				return err
			}
			m.logger.Info(
				"delaying scheduled renewal to avoid DNS update collisions",
				"name", certificate.Name,
				"delay", delay,
			)
			timer := time.NewTimer(delay)
			defer timer.Stop()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-timer.C:
			}
		}

		profile, ok := cfg.Profile(certificate.Profile)
		if !ok {
			return fmt.Errorf("profile %q disappeared", certificate.Profile)
		}

		var err error
		issueCtx, cancel := context.WithTimeout(
			ctx,
			profile.ProviderOptions.PropagationTimeout.Duration+30*time.Minute,
		)
		files, err = m.issue(issueCtx, cfg.ACME, profile, certificate)
		cancel()
		if err != nil {
			return err
		}
		if err := m.storeCertificate(certificate.Name, files); err != nil {
			return err
		}
		m.logger.Info("certificate issued", "name", certificate.Name)
	} else {
		m.logger.Info(
			"certificate does not need renewal",
			"name", certificate.Name,
			"expires", leaf.NotAfter,
		)
	}

	return m.deployTargets(ctx, certificate, files, reason != "schedule")
}

func (m *Manager) issue(
	ctx context.Context,
	acmeConfig config.ACMEConfig,
	profile config.Profile,
	certificate config.Certificate,
) (map[string][]byte, error) {
	accountKey, err := m.accountKey(acmeConfig.ServerURL())
	if err != nil {
		return nil, err
	}

	client := &acme.Client{
		Key:          accountKey,
		HTTPClient:   directHTTPClient(),
		DirectoryURL: acmeConfig.ServerURL(),
		UserAgent:    "homehub-acme-courier/1",
	}
	if _, err := client.GetReg(ctx, ""); errors.Is(err, acme.ErrNoAccount) {
		account := &acme.Account{
			Contact: []string{"mailto:" + acmeConfig.EmailAccount},
		}
		if _, err := client.Register(ctx, account, acme.AcceptTOS); err != nil &&
			!errors.Is(err, acme.ErrAccountAlreadyExists) {
			return nil, fmt.Errorf("register ACME account: %w", err)
		}
	} else if err != nil {
		return nil, fmt.Errorf("load ACME account: %w", err)
	}

	order, err := client.AuthorizeOrder(ctx, acme.DomainIDs(certificate.Domains...))
	if err != nil {
		return nil, fmt.Errorf("create ACME order: %w", err)
	}

	provider, err := namecheap.New(namecheap.Options{
		Username: profile.ProviderOptions.AuthUsername,
		Token:    profile.ProviderOptions.AuthToken,
		ClientIP: profile.ProviderOptions.AuthClientIP,
		TTL:      profile.ProviderOptions.TTL,
	}, m.proxy)
	if err != nil {
		return nil, err
	}

	var records []challengeRecord
	cleanup := func() {
		for _, record := range records {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			if err := provider.Cleanup(cleanupCtx, record.name, record.value); err != nil {
				m.logger.Warn("failed to remove DNS challenge", "name", record.name, "error", err)
			}
			cancel()
		}
	}
	defer cleanup()

	for _, authorizationURL := range order.AuthzURLs {
		authorization, err := client.GetAuthorization(ctx, authorizationURL)
		if err != nil {
			return nil, fmt.Errorf("get ACME authorization: %w", err)
		}
		if authorization.Status == acme.StatusValid {
			continue
		}
		if authorization.Status != acme.StatusPending {
			return nil, fmt.Errorf(
				"authorization for %s has unexpected status %s",
				authorization.Identifier.Value,
				authorization.Status,
			)
		}

		challenge := dnsChallenge(authorization.Challenges)
		if challenge == nil {
			return nil, fmt.Errorf("authorization for %s has no dns-01 challenge", authorization.Identifier.Value)
		}
		value, err := client.DNS01ChallengeRecord(challenge.Token)
		if err != nil {
			return nil, fmt.Errorf("calculate DNS challenge value: %w", err)
		}
		name := "_acme-challenge." + strings.TrimPrefix(authorization.Identifier.Value, "*.")
		if err := provider.Present(ctx, name, value); err != nil {
			return nil, fmt.Errorf("create DNS challenge %s: %w", name, err)
		}
		records = append(records, challengeRecord{
			name:          name,
			value:         value,
			authorization: authorizationURL,
			challenge:     challenge,
		})
	}

	for _, record := range records {
		if err := waitForTXT(
			ctx,
			record.name,
			record.value,
			profile.ProviderOptions.PropagationTimeout.Duration,
			profile.ProviderOptions.PollingInterval.Duration,
			func(repairCtx context.Context) error {
				return provider.Present(repairCtx, record.name, record.value)
			},
		); err != nil {
			return nil, err
		}
	}

	for _, record := range records {
		if _, err := client.Accept(ctx, record.challenge); err != nil {
			return nil, fmt.Errorf("accept DNS challenge for %s: %w", record.name, err)
		}
	}
	for _, record := range records {
		if _, err := client.WaitAuthorization(ctx, record.authorization); err != nil {
			return nil, fmt.Errorf("validate DNS challenge for %s: %w", record.name, err)
		}
	}

	order, err = client.WaitOrder(ctx, order.URI)
	if err != nil {
		return nil, fmt.Errorf("wait for ACME order: %w", err)
	}

	privateKey, privateKeyPEM, err := m.certificateKey(certificate)
	if err != nil {
		return nil, err
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject:  pkix.Name{CommonName: strings.TrimPrefix(certificate.Domains[0], "*.")},
		DNSNames: certificate.Domains,
	}, privateKey)
	if err != nil {
		return nil, fmt.Errorf("create certificate request: %w", err)
	}

	derChain, _, err := client.CreateOrderCert(ctx, order.FinalizeURL, csrDER, true)
	if err != nil {
		return nil, fmt.Errorf("finalize ACME order: %w", err)
	}
	files, err := encodeCertificateFiles(derChain, privateKeyPEM)
	if err != nil {
		return nil, err
	}
	if _, err := validateCertificateFiles(files, certificate.Domains); err != nil {
		return nil, err
	}

	return files, nil
}

func (m *Manager) accountKey(directoryURL string) (crypto.Signer, error) {
	sum := sha256.Sum256([]byte(directoryURL))
	directory := filepath.Join(m.storage, ".acme-courier", "accounts")
	path := filepath.Join(directory, hex.EncodeToString(sum[:8])+".key")

	if raw, err := os.ReadFile(path); err == nil {
		key, err := parsePrivateKey(raw)
		if err != nil {
			return nil, fmt.Errorf("parse ACME account key: %w", err)
		}
		return key, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read ACME account key: %w", err)
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate ACME account key: %w", err)
	}
	der, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("encode ACME account key: %w", err)
	}
	raw := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der})

	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, fmt.Errorf("create ACME account directory: %w", err)
	}
	if err := atomicWrite(path, raw, 0o600); err != nil {
		return nil, err
	}
	return key, nil
}

func (m *Manager) certificateKey(certificate config.Certificate) (crypto.Signer, []byte, error) {
	if certificate.ReuseKey {
		path := filepath.Join(m.storage, "live", certificate.Name, "privkey.pem")
		if raw, err := os.ReadFile(path); err == nil {
			key, err := parsePrivateKey(raw)
			if err != nil {
				return nil, nil, fmt.Errorf("parse reusable certificate key: %w", err)
			}
			return key, raw, nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, nil, fmt.Errorf("read reusable certificate key: %w", err)
		}
	}

	switch certificate.KeyType {
	case "ecdsa":
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return nil, nil, fmt.Errorf("generate ECDSA certificate key: %w", err)
		}
		der, err := x509.MarshalECPrivateKey(key)
		if err != nil {
			return nil, nil, fmt.Errorf("encode ECDSA certificate key: %w", err)
		}
		return key, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}), nil
	default:
		key, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			return nil, nil, fmt.Errorf("generate RSA certificate key: %w", err)
		}
		return key, pem.EncodeToMemory(&pem.Block{
			Type:  "RSA PRIVATE KEY",
			Bytes: x509.MarshalPKCS1PrivateKey(key),
		}), nil
	}
}

func (m *Manager) loadCertificate(name string) (map[string][]byte, *x509.Certificate, error) {
	directory := filepath.Join(m.storage, "live", name)
	files := make(map[string][]byte, 4)
	for _, filename := range []string{"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"} {
		raw, err := os.ReadFile(filepath.Join(directory, filename))
		if err != nil {
			return nil, nil, err
		}
		files[filename] = raw
	}
	leaf, err := validateCertificateFiles(files, nil)
	if err != nil {
		return nil, nil, err
	}
	return files, leaf, nil
}

func (m *Manager) storeCertificate(name string, files map[string][]byte) error {
	directory := filepath.Join(m.storage, "live", name)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return fmt.Errorf("create certificate directory: %w", err)
	}
	for _, filename := range []string{"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"} {
		mode := os.FileMode(0o644)
		if filename == "privkey.pem" {
			mode = 0o600
		}
		if err := atomicWrite(filepath.Join(directory, filename), files[filename], mode); err != nil {
			return err
		}
	}
	return nil
}

func (m *Manager) deployTargets(
	ctx context.Context,
	certificate config.Certificate,
	files map[string][]byte,
	force bool,
) error {
	var failures []error

	for _, target := range certificate.Deploy {
		markerPath, expectedMarker, err := m.deploymentMarker(certificate.Name, target, files)
		if err != nil {
			failures = append(failures, err)
			continue
		}

		if !force {
			currentMarker, err := os.ReadFile(markerPath)
			if err == nil && strings.TrimSpace(string(currentMarker)) == expectedMarker {
				continue
			}
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				failures = append(failures, fmt.Errorf("read deployment marker: %w", err))
				continue
			}
		}

		if err := m.deployer.Deploy(ctx, []config.DeployTarget{target}, files); err != nil {
			failures = append(failures, err)
			continue
		}
		if err := os.MkdirAll(filepath.Dir(markerPath), 0o750); err != nil {
			failures = append(failures, fmt.Errorf("create deployment marker directory: %w", err))
			continue
		}
		if err := atomicWrite(markerPath, []byte(expectedMarker+"\n"), 0o600); err != nil {
			failures = append(failures, fmt.Errorf("write deployment marker: %w", err))
		}
	}

	return errors.Join(failures...)
}

func (m *Manager) deploymentMarker(
	certificateName string,
	target config.DeployTarget,
	files map[string][]byte,
) (path, value string, err error) {
	targetHash := sha256.New()
	writeHashField(targetHash, target.Path)
	writeHashField(targetHash, target.IdentityFile)
	writeHashField(targetHash, target.KnownHostsFile)
	writeHashField(targetHash, strconv.Itoa(target.Port))
	for _, name := range target.SelectedFiles() {
		writeHashField(targetHash, name)
	}
	targetID := hex.EncodeToString(targetHash.Sum(nil)[:12])

	contentHash := sha256.New()
	for _, name := range target.SelectedFiles() {
		content, ok := files[name]
		if !ok {
			return "", "", fmt.Errorf("generated file %q is unavailable", name)
		}
		writeHashField(contentHash, name)
		_, _ = contentHash.Write(content)
	}

	return filepath.Join(m.storage, ".acme-courier", "deployments", certificateName, targetID),
		hex.EncodeToString(contentHash.Sum(nil)),
		nil
}

func writeHashField(digest hash.Hash, value string) {
	_, _ = digest.Write([]byte(value))
	_, _ = digest.Write([]byte{0})
}

func dnsChallenge(challenges []*acme.Challenge) *acme.Challenge {
	for _, challenge := range challenges {
		if challenge.Type == "dns-01" {
			return challenge
		}
	}
	return nil
}

func directHTTPClient() *http.Client {
	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return &http.Client{Timeout: 2 * time.Minute}
	}
	transport = transport.Clone()
	transport.Proxy = nil
	return &http.Client{
		Timeout:   2 * time.Minute,
		Transport: transport,
	}
}

func waitForTXT(
	ctx context.Context,
	name, expected string,
	timeout, interval time.Duration,
	repair func(context.Context) error,
) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	repairInterval := max(time.Minute, interval*4)
	nextRepair := time.Now().Add(repairInterval)

	for {
		values, err := net.DefaultResolver.LookupTXT(ctx, name)
		if err == nil {
			for _, value := range values {
				if value == expected {
					return nil
				}
			}
		}

		if !time.Now().Before(nextRepair) {
			repairCtx, repairCancel := context.WithTimeout(ctx, 2*time.Minute)
			err := repair(repairCtx)
			repairCancel()
			if err != nil {
				return fmt.Errorf("repair DNS challenge %s: %w", name, err)
			}
			nextRepair = time.Now().Add(repairInterval)
		}

		select {
		case <-ctx.Done():
			return fmt.Errorf("wait for DNS propagation of %s: %w", name, ctx.Err())
		case <-ticker.C:
		}
	}
}

func randomDelay(maximum time.Duration) (time.Duration, error) {
	if maximum <= 0 {
		return 0, nil
	}
	value, err := rand.Int(rand.Reader, big.NewInt(int64(maximum)+1))
	if err != nil {
		return 0, fmt.Errorf("generate renewal jitter: %w", err)
	}
	return time.Duration(value.Int64()), nil
}

func encodeCertificateFiles(derChain [][]byte, privateKey []byte) (map[string][]byte, error) {
	if len(derChain) == 0 {
		return nil, errors.New("ACME server returned an empty certificate chain")
	}

	var leaf, chain, full bytes.Buffer
	for i, der := range derChain {
		block := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
		full.Write(block)
		if i == 0 {
			leaf.Write(block)
		} else {
			chain.Write(block)
		}
	}

	return map[string][]byte{
		"cert.pem":      leaf.Bytes(),
		"chain.pem":     chain.Bytes(),
		"fullchain.pem": full.Bytes(),
		"privkey.pem":   privateKey,
	}, nil
}

func validateCertificateFiles(files map[string][]byte, expectedDomains []string) (*x509.Certificate, error) {
	block, _ := pem.Decode(files["cert.pem"])
	if block == nil || block.Type != "CERTIFICATE" {
		return nil, errors.New("cert.pem does not contain a certificate")
	}
	leaf, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse certificate: %w", err)
	}

	key, err := parsePrivateKey(files["privkey.pem"])
	if err != nil {
		return nil, fmt.Errorf("parse certificate private key: %w", err)
	}
	certificatePublicKey, err := x509.MarshalPKIXPublicKey(leaf.PublicKey)
	if err != nil {
		return nil, fmt.Errorf("encode certificate public key: %w", err)
	}
	privatePublicKey, err := x509.MarshalPKIXPublicKey(key.Public())
	if err != nil {
		return nil, fmt.Errorf("encode private-key public key: %w", err)
	}
	if !bytes.Equal(certificatePublicKey, privatePublicKey) {
		return nil, errors.New("certificate does not match private key")
	}

	if len(expectedDomains) > 0 && !sameDomains(leaf.DNSNames, expectedDomains) {
		return nil, fmt.Errorf(
			"certificate domains %v do not match requested domains %v",
			leaf.DNSNames,
			expectedDomains,
		)
	}
	return leaf, nil
}

func shouldRenew(
	certificate *x509.Certificate,
	expectedDomains []string,
	renewBeforeDays int,
	now time.Time,
) bool {
	if !sameDomains(certificate.DNSNames, expectedDomains) {
		return true
	}
	return !certificate.NotAfter.After(now.Add(time.Duration(renewBeforeDays) * 24 * time.Hour))
}

func sameDomains(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	normalize := func(values []string) []string {
		result := make([]string, len(values))
		for i, value := range values {
			result[i] = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(value), "."))
		}
		sort.Strings(result)
		return result
	}
	left = normalize(left)
	right = normalize(right)
	return strings.Join(left, "\x00") == strings.Join(right, "\x00")
}

func parsePrivateKey(raw []byte) (crypto.Signer, error) {
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("private key is not PEM encoded")
	}

	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	if key, err := x509.ParseECPrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, errors.New("unsupported private key format")
	}
	signer, ok := key.(crypto.Signer)
	if !ok {
		return nil, errors.New("private key cannot sign")
	}
	return signer, nil
}

func atomicWrite(path string, content []byte, mode os.FileMode) error {
	directory := filepath.Dir(path)
	file, err := os.CreateTemp(directory, "."+filepath.Base(path)+".*")
	if err != nil {
		return fmt.Errorf("create temporary file for %s: %w", path, err)
	}
	tempPath := file.Name()
	defer os.Remove(tempPath)

	if err := file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(content); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}
