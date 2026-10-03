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

type problem struct {
	Title     string `json:"title"`
	Status    int    `json:"status"`
	Detail    string `json:"detail,omitempty"`
	Instance  string `json:"instance,omitempty"`
	RequestID string `json:"request_id,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	respond(w, status, contentTypeJSON, v)
}

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
	body, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Errorf("encode %T response: %w", v, err))
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(status)
	_, _ = w.Write(append(body, '\n'))
}
