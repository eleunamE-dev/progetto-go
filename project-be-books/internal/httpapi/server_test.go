package httpapi_test

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

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/httpapi"
	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/logging"
)

type problem struct {
	Title     string `json:"title"`
	Status    int    `json:"status"`
	Detail    string `json:"detail"`
	Instance  string `json:"instance"`
	RequestID string `json:"request_id"`
}

func newServer(t *testing.T) (http.Handler, *bytes.Buffer) {
	t.Helper()
	var logs bytes.Buffer
	return httpapi.NewServer(logging.New(&logs, slog.LevelDebug)), &logs
}

func serve(h http.Handler, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decode[T any](t *testing.T, rec *httptest.ResponseRecorder) T {
	t.Helper()
	var v T
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &v), "body: %s", rec.Body)
	return v
}

func TestHealthz(t *testing.T) {
	t.Parallel()
	srv, _ := newServer(t)

	rec := serve(srv, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, "application/json", rec.Header().Get("Content-Type"))
	assert.JSONEq(t, `{"status":"ok"}`, rec.Body.String())
}

func TestRoutingErrorsAreProblemDocuments(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		method     string
		target     string
		wantStatus int
		wantAllow  string
	}{
		{name: "unknown path", method: http.MethodGet, target: "/nope", wantStatus: http.StatusNotFound},
		{name: "unsupported method", method: http.MethodDelete, target: "/healthz", wantStatus: http.StatusMethodNotAllowed, wantAllow: "GET, HEAD"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			srv, _ := newServer(t)

			rec := serve(srv, httptest.NewRequest(tt.method, tt.target, nil))

			require.Equal(t, tt.wantStatus, rec.Code)
			assert.Equal(t, "application/problem+json", rec.Header().Get("Content-Type"))
			assert.Equal(t, tt.wantAllow, rec.Header().Get("Allow"))
			assert.Equal(t, problem{
				Title:     http.StatusText(tt.wantStatus),
				Status:    tt.wantStatus,
				Instance:  tt.target,
				RequestID: rec.Header().Get("X-Request-ID"),
			}, decode[problem](t, rec))
		})
	}
}

func TestPathCleaningRedirectIsKept(t *testing.T) {
	t.Parallel()
	srv, _ := newServer(t)

	rec := serve(srv, httptest.NewRequest(http.MethodGet, "/a/../nope", nil))

	assert.True(t, rec.Code >= 300 && rec.Code < 400, "got status %d, want a redirect", rec.Code)
	assert.Equal(t, "/nope", rec.Header().Get("Location"))
	assert.NotEqual(t, "application/problem+json", rec.Header().Get("Content-Type"))
}

func TestRequestID(t *testing.T) {
	t.Parallel()

	t.Run("generated when the caller sends none", func(t *testing.T) {
		t.Parallel()
		srv, _ := newServer(t)

		rec := serve(srv, httptest.NewRequest(http.MethodGet, "/healthz", nil))

		assert.NotEmpty(t, rec.Header().Get("X-Request-ID"))
	})

	t.Run("caller's ID is kept", func(t *testing.T) {
		t.Parallel()
		srv, _ := newServer(t)
		req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
		req.Header.Set("X-Request-ID", "req-42.retry_1")

		rec := serve(srv, req)

		assert.Equal(t, "req-42.retry_1", rec.Header().Get("X-Request-ID"))
	})

	for name, id := range map[string]string{
		"with unsafe characters": "id\" injected=\"1",
		"too long":               strings.Repeat("a", 65),
	} {
		t.Run("caller's ID "+name+" is replaced", func(t *testing.T) {
			t.Parallel()
			srv, _ := newServer(t)
			req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
			req.Header.Set("X-Request-ID", id)

			rec := serve(srv, req)

			got := rec.Header().Get("X-Request-ID")
			assert.NotEmpty(t, got)
			assert.NotEqual(t, id, got)
		})
	}
}

func TestAccessLog(t *testing.T) {
	t.Parallel()
	srv, logs := newServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz?verbose=1", nil)
	req.Header.Set("X-Request-ID", "req-42")

	serve(srv, req)

	var record map[string]any
	require.NoError(t, json.Unmarshal(logs.Bytes(), &record), "logs: %s", logs)
	assert.Equal(t, "http request", record["msg"])
	assert.Equal(t, "INFO", record["level"])
	assert.Equal(t, "GET", record["method"])
	assert.Equal(t, "/healthz", record["path"], "the query string is not logged")
	assert.InDelta(t, http.StatusOK, record["status"], 0)
	assert.Equal(t, "req-42", record["request_id"])
	assert.Contains(t, record, "duration_ms")
}
