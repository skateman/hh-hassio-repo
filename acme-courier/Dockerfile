ARG BUILD_ARCH=amd64
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base

FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS builder

ARG BUILD_ARCH

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY cmd/ cmd/
COPY internal/ internal/
RUN case "$BUILD_ARCH" in \
      amd64) goarch=amd64 ;; \
      aarch64) goarch=arm64 ;; \
      *) echo "Unsupported architecture: $BUILD_ARCH" >&2; exit 1 ;; \
    esac && \
    CGO_ENABLED=0 GOARCH="$goarch" GOOS=linux \
      go build -trimpath -ldflags="-s -w" -o /out/acme-courier ./cmd/acme-courier

FROM $BUILD_FROM

RUN apk add --no-cache ca-certificates openssh-client

COPY --from=builder /out/acme-courier /usr/bin/acme-courier

CMD [ "/usr/bin/acme-courier" ]

LABEL io.hass.type="addon"
