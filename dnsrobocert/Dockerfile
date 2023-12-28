ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM

ENV CONFIG_PATH="/config/config.yml"
ENV CERTS_PATH="/config/letsencrypt"

RUN apk update                                                   && \
    apk add --no-cache gcc libffi-dev libc-dev libxslt curl bash && \
    pip install dnsrobocert                                      && \
    apk del gcc libc-dev libffi-dev

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
LABEL io.hass.version="VERSION" io.hass.type="addon" io.hass.arch="aarch64|i386|amd64"
