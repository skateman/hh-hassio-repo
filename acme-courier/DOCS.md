# ACME Courier

ACME Courier obtains and renews Let's Encrypt certificates through Namecheap
DNS-01 challenges. It can deploy generated PEM files to any number of local
directories and SSH targets.

All settings are configured in the add-on's **Configuration** tab, using either
the form or YAML editor. Home Assistant stores them in `/data/options.json`.
The `/config` mapping is reserved for ACME state and SSH files.

## Configuration

```yaml
draft: false

acme:
  email_account: admin@example.com
  crontab_renew: "12 01 * * *"
  directory_url: ""
  staging: false
  renew_before_days: 30
  renew_jitter: 30m

namecheap:
  auth_username: account-name
  auth_token: namecheap-api-token
  auth_client_ip: 127.0.0.1
  proxy: http://proxy.example.com:8080
  ttl: 120
  propagation_timeout: 1h
  polling_interval: 15s

certificates:
  - name: home.example.com
    domains:
      - home.example.com
      - "*.home.example.com"
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

`namecheap.proxy` is optional and is used only for Namecheap API traffic.
Requests to Let's Encrypt and SSH targets remain direct. Proxy credentials are
masked by Home Assistant because the field uses the `password` schema type.

If `namecheap.auth_client_ip` is empty, ACME Courier discovers the outbound
address through the same Namecheap proxy. Set it explicitly when the proxy
expects a fixed Namecheap-whitelisted value such as `127.0.0.1`.

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
- checks TXT propagation directly against every authoritative nameserver,
  bypassing stale Docker or Home Assistant DNS caches;
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
add-on configuration directory. Enter the converted configuration in ACME
Courier's **Configuration** tab. Copy the `.ssh` directory and optionally the
existing `letsencrypt` directory from the DNSRoboCert add-on configuration
directory into the ACME Courier directory.

The old example:

```yaml
deploy_hook: |
  cp -f /config/letsencrypt/live/$DNSROBOCERT_CERTIFICATE_NAME/*.pem /ssl/
  scp -i /config/.ssh/id_ed25519 /ssl/fullchain.pem /ssl/privkey.pem \
    homeassistant@10.0.0.2:/etc/ssl/homeassistant/
```

becomes:

```yaml
namecheap:
  auth_username: account-name
  auth_token: namecheap-api-token
  auth_client_ip: 127.0.0.1
  proxy: http://proxy.example.com:8080

certificates:
  - name: home.example.com
    domains:
      - home.example.com
      - "*.home.example.com"
    force_renew: false
    reuse_key: false
    key_type: rsa
    deploy:
      - path: /ssl
      - path: homeassistant@10.0.0.2:/etc/ssl/homeassistant/
        identity_file: /config/.ssh/id_ed25519
        known_hosts_file: /config/.ssh/known_hosts
        files:
          - fullchain.pem
          - privkey.pem
```

The DNSRoboCert `profiles` list and each certificate's `profile` field are not
used: ACME Courier has exactly one top-level `namecheap` configuration.

## Migrating from ACME Courier 1.0.0

Version 1.1.0 moves every setting into the Home Assistant add-on options and is
marked as a breaking update.

1. Copy the contents of `/config/config.yml` into the add-on Configuration YAML
   editor.
2. Replace `profiles` with the top-level `namecheap` mapping shown above.
3. Remove every certificate's `profile` field.
4. Move the old top-level add-on `proxy` option to `namecheap.proxy`.
5. Keep `/config/.ssh` and `/config/letsencrypt` in place.

For example, the deployment portion remains:

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
