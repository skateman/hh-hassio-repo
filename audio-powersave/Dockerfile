ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apk add --no-cache ruby ruby-concurrent-ruby-ext pulseaudio-utils

COPY run.rb /
RUN chmod a+x /run.rb

CMD [ "/run.rb" ]

LABEL io.hass.version="VERSION" io.hass.type="addon" io.hass.arch="armhf|aarch64"
