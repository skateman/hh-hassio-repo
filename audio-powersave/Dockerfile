ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base
FROM $BUILD_FROM

ENV LANG=C.UTF-8

RUN apk add --no-cache pulseaudio-utils ruby ruby-concurrent-ruby-ext

COPY run.rb /
RUN chmod a+x /run.rb

CMD [ "/run.rb" ]

LABEL io.hass.type="addon"
