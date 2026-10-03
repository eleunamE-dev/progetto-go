FROM golang:1.23

WORKDIR /app

COPY go.mod .
RUN go mod download

COPY . .
ENV CGO_ENABLED=0 GOOS=linux

CMD ["go", "test", "."]
