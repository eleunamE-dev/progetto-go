// Package config loads the service configuration from environment variables.
package config

import (
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/caarlos0/env/v11"
)

// Config is the configuration of the API server.
type Config struct {
	LogLevel slog.Level `env:"LOG_LEVEL" envDefault:"info"`
	HTTP     HTTP       `envPrefix:"HTTP_"`
}

// HTTP configures the HTTP server.
type HTTP struct {
	Addr              string        `env:"ADDR" envDefault:":8080"`
	ReadHeaderTimeout time.Duration `env:"READ_HEADER_TIMEOUT" envDefault:"5s"`
	ReadTimeout       time.Duration `env:"READ_TIMEOUT" envDefault:"10s"`
	WriteTimeout      time.Duration `env:"WRITE_TIMEOUT" envDefault:"30s"`
	IdleTimeout       time.Duration `env:"IDLE_TIMEOUT" envDefault:"120s"`
	ShutdownTimeout   time.Duration `env:"SHUTDOWN_TIMEOUT" envDefault:"15s"`
}

// Load reads the configuration from the process environment.
func Load() (Config, error) {
	return LoadFrom(env.ToMap(os.Environ()))
}

// LoadFrom reads the configuration from the given variables.
func LoadFrom(environ map[string]string) (Config, error) {
	cfg, err := env.ParseAsWithOptions[Config](env.Options{Environment: environ})
	if err != nil {
		return Config{}, fmt.Errorf("load config: %w", err)
	}
	return cfg, nil
}
