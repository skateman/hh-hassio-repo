ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM AS constraints

RUN apk update && apk add gcc py3-cffi libffi-dev libc-dev git && apk cache clean

RUN pip install --break-system-packages "poetry==1.7.1"

RUN cd /tmp                                                                                    && \
    git clone --no-checkout https://github.com/adferrand/dnsrobocert.git                       && \
    cd /tmp/dnsrobocert                                                                        && \
    git checkout $(git describe --tags `git rev-list --tags --max-count=1`)                    && \
    poetry export --format constraints.txt --without-hashes > /tmp/dnsrobocert/constraints.txt && \
    poetry build -f wheel

FROM $BUILD_FROM

COPY --from=constraints /tmp/dnsrobocert/constraints.txt /tmp/dnsrobocert/dist/*.whl /tmp/dnsrobocert/

ENV CONFIG_PATH /etc/dnsrobocert/config.yml
ENV CERTS_PATH /etc/letsencrypt

RUN apk update                                              && \
    apk add curl bash libxslt                               && \
    ln -s /config/dnsrobocert /etc/dnsrobocert              && \
    ln -s /config/letsencrypt /etc/letsencrypt              && \
    mkdir -p /opt/dnsrobocert/bin                           && \
    ln -s /usr/local/bin/python /opt/dnsrobocert/bin/python && \
    PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple python3 -m pip install -c /tmp/dnsrobocert/constraints.txt /tmp/dnsrobocert/*.whl && rm -rf /tmp && apk cache clean

COPY --from=constraints /tmp/dnsrobocert/docker/run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
LABEL io.hass.version="VERSION" io.hass.type="addon" io.hass.arch="aarch64|i386|amd64"
