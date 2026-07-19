ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM

# dnsrobocert pulls dns-lexicon[full], which drags in every provider SDK.
# The Oracle Cloud (oci) and Tencent Cloud SDKs alone add ~770 MB, so drop
# them again; the remaining providers (namecheap, route53, ...) keep working.
RUN apk add --no-cache gcc libffi-dev libc-dev libxslt curl bash openssh && \
    pip install --no-cache-dir dnsrobocert                               && \
    pip uninstall -y oci tencentcloud-sdk-python                         && \
    apk del gcc libc-dev libffi-dev

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL io.hass.type="addon"
