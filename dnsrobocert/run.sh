#!/usr/bin/with-contenv bashio

mkdir -p /config/letsencrypt

export HTTPS_PROXY=$(bashio::config 'proxy')

if [ -n "$TIMEZONE" ]; then
  if [ -f /usr/share/zoneinfo/"$TIMEZONE" ]; then
    echo "Setting timezone to $TIMEZONE"
    cp /usr/share/zoneinfo/"$TIMEZONE" /etc/localtime
    echo "$TIMEZONE" > /etc/timezone
  else
    echo "$TIMEZONE does not exist"
  fi
fi

exec dnsrobocert -c /config/config.yml -d /config/letsencrypt
