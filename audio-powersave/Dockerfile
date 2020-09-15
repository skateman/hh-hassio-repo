ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apk add --no-cache pulseaudio-utils ruby ruby-concurrent-ruby-ext ruby-json

COPY run.rb /
RUN chmod a+x /run.rb

CMD [ "/run.rb" ]

LABEL io.hass.version="VERSION" io.hass.type="addon" io.hass.arch="armv7|armhf|aarch64|i386|amd64"
