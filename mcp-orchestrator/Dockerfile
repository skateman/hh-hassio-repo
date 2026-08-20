ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base-python

FROM $BUILD_FROM

RUN apk update                                                           && \
    apk add --no-cache gcc libffi-dev musl-dev                           && \
    pip install --no-cache-dir 'mcp>=1,<3' openai fastapi uvicorn httpx azure-storage-blob aiohttp && \
    apk del gcc libffi-dev musl-dev

COPY src/ /app/
WORKDIR /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL io.hass.type="addon"
