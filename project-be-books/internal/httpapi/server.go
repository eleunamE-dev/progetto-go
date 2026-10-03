// Package httpapi implements the HTTP API of the service.
package httpapi

import (
	"log/slog"
	"net/http"
)

// NewServer returns the HTTP handler of the API.
func NewServer(logger *slog.Logger, books BookSearcher) http.Handler {
	mux := http.NewServeMux()
	addRoutes(mux, logger, books)

	handler := withRoutingProblems(mux)
	handler = recoverPanics(logger)(handler)
	handler = logRequests(logger)(handler)
	handler = withRequestID(handler)
	return handler
}

func addRoutes(mux *http.ServeMux, logger *slog.Logger, books BookSearcher) {
	mux.Handle("GET /healthz", handleHealthz())
	mux.Handle("GET /book/search", handleBookSearch(logger, books))
}

func handleHealthz() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

func withRoutingProblems(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, pattern := mux.Handler(r); pattern == "" {
			w = &problemWriter{ResponseWriter: w, r: r}
		}
		mux.ServeHTTP(w, r)
	})
}

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
	return len(b), nil
}
