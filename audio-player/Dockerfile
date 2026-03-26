ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base
FROM $BUILD_FROM

ENV LANG=C.UTF-8

RUN apk add --no-cache jq sox alsa-plugins-pulse

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL io.hass.type="addon"
