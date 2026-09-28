# ACME Courier

ACME Courier obtains and renews Let's Encrypt certificates through Namecheap
DNS-01 challenges. It can deploy generated PEM files to any number of local
directories and SSH targets.

The add-on reads `/config/config.yml` and keeps its ACME account and certificate
state under `/config/letsencrypt`.

## Add-on option

`proxy` is an optional HTTP proxy URL used only for Namecheap API traffic. ACME
requests to Let's Encrypt and SSH connections do not use it.

## Configuration

```yaml
acme:
  email_account: admin@example.com
  crontab_renew: "12 01 * * *"
  renew_before_days: 30
  renew_jitter: 30m

profiles:
  - name: namecheap
    provider: namecheap
    provider_options:
      auth_username: account-name
      auth_token: namecheap-api-token
      auth_client_ip: 127.0.0.1
      ttl: 120
      propagation_timeout: 1h
      polling_interval: 15s

certificates:
  - name: home.example.com
    domains:
      - home.example.com
      - "*.home.example.com"
    profile: namecheap
    force_renew: false
    reuse_key: false
    key_type: rsa
    deploy:
      - path: /ssl
        files:
          - fullchain.pem
          - privkey.pem

      - path: homeassistant@10.0.0.2:/etc/ssl/homeassistant/
        identity_file: /config/.ssh/id_ed25519
        known_hosts_file: /config/.ssh/known_hosts
        files:
          - fullchain.pem
          - privkey.pem
```

The cron expression uses the standard five-field format:
`minute hour day-of-month month day-of-week`. It is evaluated in the
container's local timezone, which is UTC unless the container environment
explicitly overrides it.

`renew_jitter` adds a random delay after each scheduled run. Its default is
30 minutes; set it to `0s` to disable it. The delay reduces simultaneous
whole-zone updates when several ACME Courier instances use the same Namecheap
domain.

Set `staging: true` under `acme` while testing. A custom ACME directory can
instead be supplied with `directory_url`.

## Concurrent Namecheap challenges

Namecheap's `setHosts` API replaces every DNS record in the domain, rather than
adding or removing a single record. ACME Courier therefore:

- re-reads and merges the complete zone before every TXT change;
- preserves multiple validation tokens on the same `_acme-challenge` name;
- verifies every update and retries if another instance overwrote it;
- periodically repairs a challenge record while waiting for DNS propagation;
- verifies challenge cleanup so concurrent removals converge safely; and
- applies renewal jitter before scheduled runs.

Namecheap does not expose an atomic compare-and-swap API, so no client can
provide a strict distributed transaction across hosts. For three independent
instances, keep jitter enabled or give them different cron times. A larger
jitter such as `2h` further reduces overlap when Namecheap propagation is slow.

## Deploy targets

Each deploy item has a `path`:

- An absolute path such as `/ssl` is a local target.
- A path in `user@host:/absolute/directory` form is an SSH target.

`files` is optional. When omitted, all generated files are deployed:

- `cert.pem`
- `chain.pem`
- `fullchain.pem`
- `privkey.pem`

SSH targets require `identity_file`. `known_hosts_file` is optional; when
provided, strict host-key verification is explicitly enabled against that file.
Without it, strict verification uses OpenSSH's default known-hosts files.
Both file paths must be absolute. `port` defaults to 22.

Files are uploaded under temporary names and renamed only after every upload
succeeds. Private keys use mode `0600`; certificate files use `0644`.
Successful deployments are recorded per target and certificate revision, so a
failed target is retried at the next scheduled run without repeatedly uploading
unchanged certificates to targets that already succeeded.

## Migration from DNSRoboCert

The Go implementation keeps using
`/config/letsencrypt/live/<certificate-name>/`. Existing valid Certbot PEM files
are reused until they enter the renewal window.

ACME Courier is a separate add-on, so Home Assistant gives it a separate
add-on configuration directory. Copy `config.yml`, the `.ssh` directory, and
optionally the existing `letsencrypt` directory from the DNSRoboCert add-on
configuration directory into the ACME Courier directory. Replace each
`deploy_hook` with structured `deploy` targets before starting ACME Courier.

The old example:

```yaml
deploy_hook: |
  cp -f /config/letsencrypt/live/$DNSROBOCERT_CERTIFICATE_NAME/*.pem /ssl/
  scp -i /config/.ssh/id_ed25519 /ssl/fullchain.pem /ssl/privkey.pem \
    homeassistant@10.0.0.2:/etc/ssl/homeassistant/
```

becomes:

```yaml
deploy:
  - path: /ssl
  - path: homeassistant@10.0.0.2:/etc/ssl/homeassistant/
    identity_file: /config/.ssh/id_ed25519
    known_hosts_file: /config/.ssh/known_hosts
    files:
      - fullchain.pem
      - privkey.pem
```

Use `force_renew: true` only for a deliberate one-time renewal, then set it
back to `false`.
