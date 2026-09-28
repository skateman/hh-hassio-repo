package deploy

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/skateman/hh-hassio-repo/acme-courier/internal/config"
)

type Runner interface {
	Run(ctx context.Context, name string, args []string, stdin io.Reader) error
}

type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, name string, args []string, stdin io.Reader) error {
	command := exec.CommandContext(ctx, name, args...)
	command.Stdin = stdin
	var stderr bytes.Buffer
	command.Stderr = &stderr
	command.Stdout = os.Stdout

	if err := command.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			return err
		}
		return fmt.Errorf("%w: %s", err, message)
	}
	return nil
}

type Deployer struct {
	runner Runner
	logger *slog.Logger
}

func New(runner Runner, logger *slog.Logger) *Deployer {
	return &Deployer{runner: runner, logger: logger}
}

func (d *Deployer) Deploy(
	ctx context.Context,
	targets []config.DeployTarget,
	files map[string][]byte,
) error {
	var failures []error
	for _, target := range targets {
		var err error
		if target.IsRemote() {
			err = d.deployRemote(ctx, target, files)
		} else {
			err = d.deployLocal(target, files)
		}
		if err != nil {
			failures = append(failures, fmt.Errorf("deploy to %q: %w", target.Path, err))
		}
	}
	return errors.Join(failures...)
}

func (d *Deployer) deployLocal(target config.DeployTarget, files map[string][]byte) error {
	if err := os.MkdirAll(target.Path, 0o750); err != nil {
		return fmt.Errorf("create target directory: %w", err)
	}

	for _, name := range target.SelectedFiles() {
		content, ok := files[name]
		if !ok {
			return fmt.Errorf("generated file %q is unavailable", name)
		}
		mode := fileMode(name)
		if err := atomicWrite(filepath.Join(target.Path, name), content, mode); err != nil {
			return err
		}
	}

	d.logger.Info("deployed certificate files locally", "path", target.Path)
	return nil
}

func (d *Deployer) deployRemote(
	ctx context.Context,
	target config.DeployTarget,
	files map[string][]byte,
) error {
	remote, err := parseRemote(target.Path)
	if err != nil {
		return err
	}
	if _, err := os.Stat(target.IdentityFile); err != nil {
		return fmt.Errorf("read identity file: %w", err)
	}
	if target.KnownHostsFile != "" {
		if _, err := os.Stat(target.KnownHostsFile); err != nil {
			return fmt.Errorf("read known_hosts file: %w", err)
		}
	}

	suffix, err := randomSuffix()
	if err != nil {
		return err
	}

	sshArgs := sshArguments(target, remote.destination())
	temporary := make(map[string]string)
	cleanup := func() {
		if len(temporary) == 0 {
			return
		}
		paths := make([]string, 0, len(temporary))
		for _, path := range temporary {
			paths = append(paths, shellQuote(path))
		}
		args := append(append([]string(nil), sshArgs...), "rm -f -- "+strings.Join(paths, " "))
		_ = d.runner.Run(context.Background(), "ssh", args, nil)
	}

	for _, name := range target.SelectedFiles() {
		content, ok := files[name]
		if !ok {
			cleanup()
			return fmt.Errorf("generated file %q is unavailable", name)
		}

		tempPath := filepath.Join(remote.directory, "."+name+".acme-courier-"+suffix)
		command := "set -eu; test -d " + shellQuote(remote.directory) +
			"; umask 077; cat > " + shellQuote(tempPath)
		args := append(append([]string(nil), sshArgs...), command)
		if err := d.runner.Run(ctx, "ssh", args, bytes.NewReader(content)); err != nil {
			cleanup()
			return fmt.Errorf("upload %s: %w", name, err)
		}
		temporary[name] = tempPath
	}

	var commands []string
	for _, name := range target.SelectedFiles() {
		tempPath := temporary[name]
		finalPath := filepath.Join(remote.directory, name)
		commands = append(commands,
			"chmod "+strconv.FormatUint(uint64(fileMode(name).Perm()), 8)+" "+shellQuote(tempPath),
			"mv -f -- "+shellQuote(tempPath)+" "+shellQuote(finalPath),
		)
	}
	args := append(append([]string(nil), sshArgs...), "set -eu; "+strings.Join(commands, "; "))
	if err := d.runner.Run(ctx, "ssh", args, nil); err != nil {
		cleanup()
		return fmt.Errorf("activate uploaded files: %w", err)
	}

	d.logger.Info(
		"deployed certificate files over SSH",
		"host", remote.host,
		"path", remote.directory,
	)
	return nil
}

func sshArguments(target config.DeployTarget, destination string) []string {
	args := []string{
		"-o", "BatchMode=yes",
		"-o", "IdentitiesOnly=yes",
		"-o", "ConnectTimeout=30",
		"-o", "StrictHostKeyChecking=yes",
		"-i", target.IdentityFile,
		"-p", strconv.Itoa(target.Port),
	}
	if target.KnownHostsFile != "" {
		args = append(args,
			"-o", "UserKnownHostsFile="+target.KnownHostsFile,
		)
	}
	return append(args, destination)
}

type remotePath struct {
	user      string
	host      string
	directory string
}

func (r remotePath) destination() string {
	return r.user + "@" + r.host
}

func parseRemote(value string) (remotePath, error) {
	at := strings.IndexByte(value, '@')
	if at < 1 {
		return remotePath{}, errors.New("remote path must use user@host:path")
	}

	remainder := value[at+1:]
	var host, directory string
	if strings.HasPrefix(remainder, "[") {
		separator := strings.Index(remainder, "]:")
		if separator < 2 {
			return remotePath{}, errors.New("remote path has an invalid bracketed host")
		}
		host = remainder[1:separator]
		directory = remainder[separator+2:]
	} else {
		colon := strings.IndexByte(remainder, ':')
		if colon < 1 {
			return remotePath{}, errors.New("remote path must use user@host:path")
		}
		host = remainder[:colon]
		directory = remainder[colon+1:]
	}

	result := remotePath{
		user:      value[:at],
		host:      host,
		directory: directory,
	}
	if result.directory == "" || !strings.HasPrefix(result.directory, "/") {
		return remotePath{}, errors.New("remote directory must be absolute")
	}
	result.directory = strings.TrimSuffix(result.directory, "/")
	return result, nil
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
		return fmt.Errorf("set permissions on %s: %w", tempPath, err)
	}
	if _, err := file.Write(content); err != nil {
		file.Close()
		return fmt.Errorf("write %s: %w", tempPath, err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("sync %s: %w", tempPath, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close %s: %w", tempPath, err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}

func fileMode(name string) os.FileMode {
	if name == "privkey.pem" {
		return 0o600
	}
	return 0o644
}

func randomSuffix() (string, error) {
	value := make([]byte, 8)
	if _, err := rand.Read(value); err != nil {
		return "", fmt.Errorf("generate temporary file suffix: %w", err)
	}
	return hex.EncodeToString(value), nil
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}
