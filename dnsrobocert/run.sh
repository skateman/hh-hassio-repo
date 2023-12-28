#!/usr/bin/with-contenv bashio

mkdir -p /config/letsencrypt

export HTTPS_PROXY="$(bashio::config 'proxy')"

exec dnsrobocert -c /config/config.yml -d /config/letsencrypt
