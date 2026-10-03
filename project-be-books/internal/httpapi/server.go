// Package httpapi exposes the service over HTTP: routing, middleware, request
// decoding and response encoding. Business logic lives in other packages.
package httpapi

import (
	"log/slog"
	"net/http"
)

// NewServer returns the HTTP handler of the API: every route, wrapped in the
// middleware shared by all requests.
func NewServer(logger *slog.Logger) http.Handler {
	mux := http.NewServeMux()
	addRoutes(mux)

	handler := withRoutingProblems(mux)
	handler = recoverPanics(logger)(handler)
	handler = logRequests(logger)(handler)
	handler = withRequestID(handler)
	return handler
}

// addRoutes maps each endpoint to its handler: it is the one place listing the
// whole HTTP surface of the service.
func addRoutes(mux *http.ServeMux) {
	mux.Handle("GET /healthz", handleHealthz())
}

// handleHealthz reports that the process is up and serving requests. It checks
// no dependency on purpose: a database outage must not get healthy API
// instances restarted.
func handleHealthz() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// withRoutingProblems makes the errors produced by the mux itself, 404 for an
// unknown path and 405 for an unsupported method, use problem documents like
// every other error of the API, instead of the default plain-text bodies.
func withRoutingProblems(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, pattern := mux.Handler(r); pattern == "" {
			w = &problemWriter{ResponseWriter: w, r: r}
		}
		mux.ServeHTTP(w, r)
	})
}

// problemWriter replaces an error body written by the mux with a problem
// document for the same status, keeping the headers it set (e.g. Allow on a
// 405). Non-error responses, such as path-cleaning redirects, pass through.
type problemWriter struct {
	http.ResponseWriter
	r           *http.Request
	wroteHeader bool
	passThrough bool
}

func (p *problemWriter) WriteHeader(status int) {
	if p.wroteHeader {
		return
	}
	p.wroteHeader = true
	if status < http.StatusBadRequest {
		p.passThrough = true
		p.ResponseWriter.WriteHeader(status)
		return
	}
	writeProblem(p.ResponseWriter, p.r, status, "")
}

func (p *problemWriter) Write(b []byte) (int, error) {
	if !p.wroteHeader {
		p.WriteHeader(http.StatusOK)
	}
	if p.passThrough {
		return p.ResponseWriter.Write(b)
	}
	return len(b), nil // the problem document has replaced the mux's body
}
