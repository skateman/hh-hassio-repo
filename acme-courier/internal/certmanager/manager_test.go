package certmanager

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"testing"
	"time"

	"github.com/miekg/dns"

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

func TestQueryTXTServer(t *testing.T) {
	t.Parallel()

	packetConn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &dns.Server{
		PacketConn: packetConn,
		Handler: dns.HandlerFunc(func(writer dns.ResponseWriter, request *dns.Msg) {
			response := new(dns.Msg)
			response.SetReply(request)
			response.Authoritative = true
			response.Answer = []dns.RR{&dns.TXT{
				Hdr: dns.RR_Header{
					Name:   request.Question[0].Name,
					Rrtype: dns.TypeTXT,
					Class:  dns.ClassINET,
					Ttl:    120,
				},
				Txt: []string{"expected-token"},
			}}
			_ = writer.WriteMsg(response)
		}),
	}
	go func() {
		_ = server.ActivateAndServe()
	}()
	t.Cleanup(func() {
		_ = server.Shutdown()
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	found, err := queryTXTServer(ctx, packetConn.LocalAddr().String(), "_acme-challenge.example.com", "expected-token")
	if err != nil {
		t.Fatal(err)
	}
	if !found {
		t.Fatal("expected TXT token was not found")
	}

	found, err = queryTXTServer(ctx, packetConn.LocalAddr().String(), "_acme-challenge.example.com", "other-token")
	if err != nil {
		t.Fatal(err)
	}
	if found {
		t.Fatal("unexpected TXT token was found")
	}
}
