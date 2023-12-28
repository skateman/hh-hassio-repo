#!/bin/sh

mkdir -p /config/letsencrypt

if [ -n "$TIMEZONE" ]; then
  if [ -f /usr/share/zoneinfo/"$TIMEZONE" ]; then
    echo "Setting timezone to $TIMEZONE"
    cp /usr/share/zoneinfo/"$TIMEZONE" /etc/localtime
    echo "$TIMEZONE" > /etc/timezone
  else
    echo "$TIMEZONE does not exist"
  fi
fi

exec dnsrobocert -c "${CONFIG_PATH}" -d "${CERTS_PATH}"
