package deploy

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/skateman/hh-hassio-repo/acme-courier/internal/config"
)

type recordedCall struct {
	name  string
	args  []string
	stdin []byte
}

type recordingRunner struct {
	calls []recordedCall
}

func (r *recordingRunner) Run(
	_ context.Context,
	name string,
	args []string,
	stdin io.Reader,
) error {
	var input []byte
	if stdin != nil {
		input, _ = io.ReadAll(stdin)
	}
	r.calls = append(r.calls, recordedCall{
		name:  name,
		args:  append([]string(nil), args...),
		stdin: input,
	})
	return nil
}

func TestDeployLocalUsesDefaultFilesAndPermissions(t *testing.T) {
	t.Parallel()

	target := t.TempDir()
	deployer := New(ExecRunner{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	files := map[string][]byte{
		"cert.pem":      []byte("cert"),
		"chain.pem":     []byte("chain"),
		"fullchain.pem": []byte("fullchain"),
		"privkey.pem":   []byte("key"),
	}

	if err := deployer.Deploy(context.Background(), []config.DeployTarget{{Path: target}}, files); err != nil {
		t.Fatal(err)
	}

	for name, expected := range files {
		actual, err := os.ReadFile(filepath.Join(target, name))
		if err != nil {
			t.Fatal(err)
		}
		if string(actual) != string(expected) {
			t.Fatalf("%s = %q", name, actual)
		}
	}

	info, err := os.Stat(filepath.Join(target, "privkey.pem"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("private key mode = %o", info.Mode().Perm())
	}
}

func TestParseRemote(t *testing.T) {
	t.Parallel()

	remote, err := parseRemote("homeassistant@10.0.0.2:/etc/ssl/homeassistant/")
	if err != nil {
		t.Fatal(err)
	}
	if remote.user != "homeassistant" || remote.host != "10.0.0.2" ||
		remote.directory != "/etc/ssl/homeassistant" {
		t.Fatalf("remote = %#v", remote)
	}
}

func TestParseRemoteIPv6(t *testing.T) {
	t.Parallel()

	remote, err := parseRemote("homeassistant@[2001:db8::1]:/etc/ssl/homeassistant/")
	if err != nil {
		t.Fatal(err)
	}
	if remote.host != "2001:db8::1" || remote.directory != "/etc/ssl/homeassistant" {
		t.Fatalf("remote = %#v", remote)
	}
}

func TestSSHArgumentsAlwaysRequireKnownHost(t *testing.T) {
	t.Parallel()

	args := sshArguments(config.DeployTarget{
		IdentityFile: "/config/.ssh/id_ed25519",
		Port:         22,
	}, "homeassistant@10.0.0.2")

	var found bool
	for i := range args {
		if args[i] == "StrictHostKeyChecking=yes" {
			found = true
		}
	}
	if !found {
		t.Fatalf("strict host-key checking missing from %#v", args)
	}
}

func TestDeployRemoteStagesThenActivatesSelectedFiles(t *testing.T) {
	t.Parallel()

	identity := filepath.Join(t.TempDir(), "id_ed25519")
	knownHosts := filepath.Join(t.TempDir(), "known_hosts")
	if err := os.WriteFile(identity, []byte("test identity"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(knownHosts, []byte("test host"), 0o600); err != nil {
		t.Fatal(err)
	}

	runner := &recordingRunner{}
	deployer := New(runner, slog.New(slog.NewTextHandler(io.Discard, nil)))
	target := config.DeployTarget{
		Path:           "homeassistant@10.0.0.2:/etc/ssl/homeassistant/",
		IdentityFile:   identity,
		KnownHostsFile: knownHosts,
		Port:           22,
		Files:          []string{"fullchain.pem", "privkey.pem"},
	}
	files := map[string][]byte{
		"fullchain.pem": []byte("certificate"),
		"privkey.pem":   []byte("private-key"),
	}

	if err := deployer.Deploy(context.Background(), []config.DeployTarget{target}, files); err != nil {
		t.Fatal(err)
	}
	if len(runner.calls) != 3 {
		t.Fatalf("calls = %#v", runner.calls)
	}
	if !bytes.Equal(runner.calls[0].stdin, files["fullchain.pem"]) {
		t.Fatalf("first upload = %q", runner.calls[0].stdin)
	}
	if !bytes.Equal(runner.calls[1].stdin, files["privkey.pem"]) {
		t.Fatalf("second upload = %q", runner.calls[1].stdin)
	}
	activation := runner.calls[2].args[len(runner.calls[2].args)-1]
	if !strings.Contains(activation, "fullchain.pem") || !strings.Contains(activation, "privkey.pem") {
		t.Fatalf("activation command = %q", activation)
	}
}
