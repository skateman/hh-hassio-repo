package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/robfig/cron/v3"

	"github.com/skateman/hh-hassio-repo/acme-courier/internal/certmanager"
	"github.com/skateman/hh-hassio-repo/acme-courier/internal/config"
	"github.com/skateman/hh-hassio-repo/acme-courier/internal/deploy"
)

type addonOptions struct {
	Proxy string `json:"proxy"`
}

func main() {
	var (
		configPath  = flag.String("config", "/config/config.yml", "configuration file")
		storagePath = flag.String("storage", "/config/letsencrypt", "certificate state directory")
		optionsPath = flag.String("options", "/data/options.json", "Home Assistant add-on options")
		once        = flag.Bool("once", false, "run one reconciliation and exit")
		debug       = flag.Bool("debug", false, "enable debug logging")
	)
	flag.Parse()

	level := slog.LevelInfo
	if *debug {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	slog.SetDefault(logger)

	proxy, err := loadProxy(*optionsPath)
	if err != nil {
		logger.Error("failed to load add-on options", "error", err)
		os.Exit(1)
	}

	deployer := deploy.New(deploy.ExecRunner{}, logger)
	manager := certmanager.New(*storagePath, proxy, deployer, logger)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if *once {
		cfg, _, err := config.Load(*configPath)
		if err != nil {
			logger.Error("failed to load configuration", "error", err)
			os.Exit(1)
		}
		if err := manager.Reconcile(ctx, cfg, "one-shot"); err != nil {
			logger.Error("reconciliation failed", "error", err)
			os.Exit(1)
		}
		return
	}

	if err := runDaemon(ctx, *configPath, manager, logger); err != nil &&
		!errors.Is(err, context.Canceled) {
		logger.Error("service stopped", "error", err)
		os.Exit(1)
	}
}

func runDaemon(
	ctx context.Context,
	configPath string,
	manager *certmanager.Manager,
	logger *slog.Logger,
) error {
	cfg, raw, err := config.Load(configPath)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(raw)

	triggers := make(chan string, 1)
	sendTrigger := func(reason string) {
		select {
		case triggers <- reason:
		default:
			logger.Info("reconciliation already pending", "reason", reason)
		}
	}

	scheduler, err := startScheduler(cfg.ACME.CronRenew, func() {
		sendTrigger("schedule")
	})
	if err != nil {
		return err
	}
	defer func() {
		scheduler.Stop()
	}()

	sendTrigger("startup")
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case reason := <-triggers:
			if err := manager.Reconcile(ctx, cfg, reason); err != nil {
				logger.Error("reconciliation failed", "reason", reason, "error", err)
			}
		case <-ticker.C:
			nextConfig, nextRaw, err := config.Load(configPath)
			if err != nil {
				logger.Error("configuration reload failed", "error", err)
				continue
			}
			nextDigest := sha256.Sum256(nextRaw)
			if nextDigest == digest {
				continue
			}

			if nextConfig.ACME.CronRenew != cfg.ACME.CronRenew {
				scheduler.Stop()
				scheduler, err = startScheduler(nextConfig.ACME.CronRenew, func() {
					sendTrigger("schedule")
				})
				if err != nil {
					return err
				}
			}

			cfg = nextConfig
			digest = nextDigest
			sendTrigger("configuration change")
		}
	}
}

func startScheduler(spec string, job func()) (*cron.Cron, error) {
	parser := cron.NewParser(cron.Minute | cron.Hour | cron.Dom | cron.Month | cron.Dow)
	scheduler := cron.New(cron.WithParser(parser))
	if _, err := scheduler.AddFunc(spec, job); err != nil {
		return nil, fmt.Errorf("schedule certificate renewal: %w", err)
	}
	scheduler.Start()
	return scheduler, nil
}

func loadProxy(path string) (string, error) {
	if value := os.Getenv("ACME_COURIER_PROXY"); value != "" {
		return value, nil
	}

	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}

	var options addonOptions
	if err := json.Unmarshal(raw, &options); err != nil {
		return "", fmt.Errorf("parse %s: %w", path, err)
	}
	return options.Proxy, nil
}
