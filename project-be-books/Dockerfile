# syntax=docker/dockerfile:1

# Build stage: compile a static binary for every command under ./cmd.
FROM golang:1.26-alpine AS build
WORKDIR /src

# Download the modules in a layer of their own: code changes do not invalidate it.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/ ./cmd/...

# Runtime stage: no shell, no package manager, unprivileged user.
FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=build /out/ /usr/local/bin/
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/api"]
