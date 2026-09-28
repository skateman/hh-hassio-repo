package certmanager

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"testing"
	"time"

	"github.com/skateman/hh-hassio-repo/acme-courier/internal/config"
)

func TestShouldRenew(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	certificate := &x509.Certificate{
		DNSNames: []string{"example.com", "*.example.com"},
		NotAfter: now.Add(31 * 24 * time.Hour),
	}
	if shouldRenew(certificate, []string{"*.example.com", "example.com"}, 30, now) {
		t.Fatal("certificate should not renew")
	}
	certificate.NotAfter = now.Add(29 * 24 * time.Hour)
	if !shouldRenew(certificate, []string{"example.com", "*.example.com"}, 30, now) {
		t.Fatal("certificate should renew")
	}
}

func TestValidateCertificateFilesRejectsMismatchedKey(t *testing.T) {
	t.Parallel()

	certificateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	otherKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}

	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "example.com"},
		DNSNames:     []string{"example.com"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &certificateKey.PublicKey, certificateKey)
	if err != nil {
		t.Fatal(err)
	}

	files := map[string][]byte{
		"cert.pem": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		"privkey.pem": pem.EncodeToMemory(&pem.Block{
			Type:  "RSA PRIVATE KEY",
			Bytes: x509.MarshalPKCS1PrivateKey(otherKey),
		}),
	}
	if _, err := validateCertificateFiles(files, []string{"example.com"}); err == nil {
		t.Fatal("expected key mismatch")
	}
}

func TestDeploymentMarkerTracksTargetAndCertificateContent(t *testing.T) {
	t.Parallel()

	manager := &Manager{storage: t.TempDir()}
	target := config.DeployTarget{
		Path:  "/ssl",
		Files: []string{"fullchain.pem", "privkey.pem"},
	}
	files := map[string][]byte{
		"fullchain.pem": []byte("certificate"),
		"privkey.pem":   []byte("private-key"),
	}

	firstPath, firstValue, err := manager.deploymentMarker("example.com", target, files)
	if err != nil {
		t.Fatal(err)
	}
	files["fullchain.pem"] = []byte("renewed-certificate")
	secondPath, secondValue, err := manager.deploymentMarker("example.com", target, files)
	if err != nil {
		t.Fatal(err)
	}

	if firstPath != secondPath {
		t.Fatalf("marker path changed with certificate content: %q != %q", firstPath, secondPath)
	}
	if firstValue == secondValue {
		t.Fatal("marker value did not change with certificate content")
	}
}
