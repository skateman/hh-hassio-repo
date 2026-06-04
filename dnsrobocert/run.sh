#!/usr/bin/with-contenv bashio

mkdir -p /config/letsencrypt

export HTTPS_PROXY="$(bashio::config 'proxy')"
export NO_PROXY=".letsencrypt.org"

exec dnsrobocert -c /config/config.yml -d /config/letsencrypt
