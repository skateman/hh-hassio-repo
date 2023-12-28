ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM

ENV CONFIG_PATH /etc/dnsrobocert/config.yml
ENV CERTS_PATH /etc/letsencrypt

RUN apk update                                                   && \
    apk add --no-cache gcc libffi-dev libc-dev libxslt curl bash && \
    pip install dnsrobocert                                      && \
    ln -s /config/dnsrobocert /etc/dnsrobocert                   && \
    ln -s /config/letsencrypt /etc/letsencrypt                   && \
    apk del gcc libffi-dev

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
LABEL io.hass.version="VERSION" io.hass.type="addon" io.hass.arch="aarch64|i386|amd64"
