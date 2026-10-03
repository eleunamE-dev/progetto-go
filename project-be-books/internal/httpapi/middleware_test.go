package httpapi

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/logging"
)

func withMiddleware(logger *slog.Logger, h http.Handler) http.Handler {
	return withRequestID(logRequests(logger)(recoverPanics(logger)(h)))
}

func TestRecoverPanics(t *testing.T) {
	t.Parallel()

	tests := map[string]struct {
		handler   http.HandlerFunc
		wantCause string
	}{
		"panicking handler": {
			handler: func(http.ResponseWriter, *http.Request) {
				panic("boom: secret internals")
			},
			wantCause: "boom: secret internals",
		},
		"response with no JSON form": {
			handler: func(w http.ResponseWriter, _ *http.Request) {
				writeJSON(w, http.StatusOK, map[string]any{"ch": make(chan int)})
			},
			wantCause: "json: unsupported type: chan int",
		},
	}
	for name, tt := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var logs bytes.Buffer
			h := withMiddleware(logging.New(&logs, slog.LevelInfo), tt.handler)

			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/explode", nil))

			require.Equal(t, http.StatusInternalServerError, rec.Code)
			assert.Equal(t, contentTypeProblem, rec.Header().Get("Content-Type"))
			var p problem
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &p))
			assert.Equal(t, "Internal Server Error", p.Title)
			assert.NotContains(t, rec.Body.String(), tt.wantCause, "no internal detail reaches the client")

			assert.Contains(t, logs.String(), `"msg":"panic serving request"`)
			assert.Contains(t, logs.String(), tt.wantCause, "the cause is logged")
			assert.Contains(t, logs.String(), `"stack":`)
			assert.Contains(t, logs.String(), `"level":"ERROR","msg":"http request"`, "the access log flags the 500")
		})
	}
}

func TestRecoverPanicsLetsAbortHandlerThrough(t *testing.T) {
	t.Parallel()
	h := recoverPanics(slog.New(slog.DiscardHandler))(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic(http.ErrAbortHandler)
	}))

	assert.PanicsWithError(t, http.ErrAbortHandler.Error(), func() {
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/", nil))
	})
}

func TestAccessLogDefaultsToStatusOK(t *testing.T) {
	t.Parallel()
	var logs bytes.Buffer
	h := withMiddleware(logging.New(&logs, slog.LevelInfo), http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/", nil))

	assert.Contains(t, logs.String(), `"status":200`)
}

func TestIsValidRequestID(t *testing.T) {
	t.Parallel()

	tests := []struct {
		id   string
		want bool
	}{
		{id: "abc-DEF_123.4", want: true},
		{id: strings.Repeat("a", maxRequestIDLength), want: true},
		{id: "", want: false},
		{id: strings.Repeat("a", maxRequestIDLength+1), want: false},
		{id: "with space", want: false},
		{id: "new\nline", want: false},
		{id: "àccented", want: false},
	}
	for _, tt := range tests {
		assert.Equal(t, tt.want, isValidRequestID(tt.id), "isValidRequestID(%q)", tt.id)
	}
}
