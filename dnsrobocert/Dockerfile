ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM

RUN apk update                                                           && \
    apk add --no-cache gcc libffi-dev libc-dev libxslt curl bash openssh && \
    pip install dnsrobocert                                              && \
    apk del gcc libc-dev libffi-dev

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL io.hass.type="addon"
