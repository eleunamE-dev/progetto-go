package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"
)

const (
	contentTypeJSON    = "application/json"
	contentTypeProblem = "application/problem+json"
)

// problem is an RFC 9457 "problem details" document: the body of every error
// response of the API. Type is omitted, which per the RFC means "about:blank",
// so Title is the standard text of the status code.
type problem struct {
	Title     string `json:"title"`
	Status    int    `json:"status"`
	Detail    string `json:"detail,omitempty"`
	Instance  string `json:"instance,omitempty"`
	RequestID string `json:"request_id,omitempty"`
}

// writeJSON sends v as a JSON response with the given status code.
func writeJSON(w http.ResponseWriter, status int, v any) {
	respond(w, status, contentTypeJSON, v)
}

// writeProblem sends a problem document for the given status code. detail is
// shown to the client, so it must never contain internal error messages.
func writeProblem(w http.ResponseWriter, r *http.Request, status int, detail string) {
	respond(w, status, contentTypeProblem, problem{
		Title:     http.StatusText(status),
		Status:    status,
		Detail:    detail,
		Instance:  r.URL.Path,
		RequestID: requestIDFrom(r.Context()),
	})
}

func respond(w http.ResponseWriter, status int, contentType string, v any) {
	// Encoding before writing the status line means a failure can still become
	// a 500. It can only happen with a value that has no JSON form, which is a
	// programming error: panic, and let recoverPanics log it with its stack.
	body, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Errorf("encode %T response: %w", v, err))
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(status)
	// A write error means the client has gone away: nothing left to do.
	_, _ = w.Write(append(body, '\n'))
}
