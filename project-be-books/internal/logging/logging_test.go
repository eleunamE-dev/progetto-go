package logging_test

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/logging"
)

func TestContextAttributes(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer
	logger := logging.New(&out, slog.LevelInfo).With("component", "test")

	ctx := logging.WithAttrs(context.Background(), slog.String("request_id", "abc"))
	ctx = logging.WithAttrs(ctx, slog.Int("attempt", 2))
	logger.InfoContext(ctx, "hello")

	var record map[string]any
	require.NoError(t, json.Unmarshal(out.Bytes(), &record))
	assert.Equal(t, "hello", record["msg"])
	assert.Equal(t, "abc", record["request_id"])
	assert.InDelta(t, 2, record["attempt"], 0)
	assert.Equal(t, "test", record["component"], "attributes added with Logger.With are kept")
}

func TestWithAttrsDoesNotLeakBetweenContexts(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer
	logger := logging.New(&out, slog.LevelInfo)

	parent := logging.WithAttrs(context.Background(), slog.String("request_id", "abc"))
	_ = logging.WithAttrs(parent, slog.String("child", "only here"))
	logger.InfoContext(parent, "parent")

	var record map[string]any
	require.NoError(t, json.Unmarshal(out.Bytes(), &record))
	assert.NotContains(t, record, "child")
}

func TestLevel(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer
	logger := logging.New(&out, slog.LevelWarn)

	logger.Info("dropped")
	assert.Empty(t, out.String())

	logger.Warn("kept")
	assert.Contains(t, out.String(), `"msg":"kept"`)
}
