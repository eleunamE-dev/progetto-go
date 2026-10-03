package config_test

import (
	"log/slog"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/config"
)

func TestLoadFrom(t *testing.T) {
	t.Parallel()

	t.Run("defaults need no configuration", func(t *testing.T) {
		t.Parallel()

		cfg, err := config.LoadFrom(map[string]string{})

		require.NoError(t, err)
		assert.Equal(t, slog.LevelInfo, cfg.LogLevel)
		assert.Equal(t, config.HTTP{
			Addr:              ":8080",
			ReadHeaderTimeout: 5 * time.Second,
			ReadTimeout:       10 * time.Second,
			WriteTimeout:      30 * time.Second,
			IdleTimeout:       120 * time.Second,
			ShutdownTimeout:   15 * time.Second,
		}, cfg.HTTP)
	})

	t.Run("environment overrides defaults", func(t *testing.T) {
		t.Parallel()

		cfg, err := config.LoadFrom(map[string]string{
			"LOG_LEVEL":          "debug",
			"HTTP_ADDR":          "127.0.0.1:9090",
			"HTTP_WRITE_TIMEOUT": "45s",
		})

		require.NoError(t, err)
		assert.Equal(t, slog.LevelDebug, cfg.LogLevel)
		assert.Equal(t, "127.0.0.1:9090", cfg.HTTP.Addr)
		assert.Equal(t, 45*time.Second, cfg.HTTP.WriteTimeout)
		assert.Equal(t, 5*time.Second, cfg.HTTP.ReadHeaderTimeout, "unset values keep their default")
	})

	t.Run("empty values fall back to defaults", func(t *testing.T) {
		t.Parallel()

		cfg, err := config.LoadFrom(map[string]string{"HTTP_ADDR": ""})

		require.NoError(t, err)
		assert.Equal(t, ":8080", cfg.HTTP.Addr)
	})

	t.Run("invalid values are rejected", func(t *testing.T) {
		t.Parallel()

		tests := map[string]struct {
			key, value string
		}{
			"malformed duration": {key: "HTTP_READ_TIMEOUT", value: "soon"},
			"unknown log level":  {key: "LOG_LEVEL", value: "loud"},
		}
		for name, tt := range tests {
			t.Run(name, func(t *testing.T) {
				t.Parallel()

				_, err := config.LoadFrom(map[string]string{tt.key: tt.value})

				require.Error(t, err)
				assert.ErrorContains(t, err, tt.value, "the error shows the offending value")
			})
		}
	})
}
