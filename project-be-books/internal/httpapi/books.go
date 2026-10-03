package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/catalog"
)

const (
	maxSearchQueryLength      = 200
	statusClientClosedRequest = 499
)

// BookSearcher searches the book catalog.
type BookSearcher interface {
	Search(ctx context.Context, query string, page int) (catalog.SearchResult, error)
}

type searchResponse struct {
	Count    int           `json:"count"`
	Page     int           `json:"page"`
	NextPage *int          `json:"next_page"`
	Results  []bookSummary `json:"results"`
}

type bookSummary struct {
	ID            int      `json:"id"`
	Title         string   `json:"title"`
	Authors       []string `json:"authors"`
	Languages     []string `json:"languages"`
	CoverURL      string   `json:"cover_url,omitempty"`
	DownloadCount int      `json:"download_count"`
}

func handleBookSearch(logger *slog.Logger, books BookSearcher) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query, page, err := parseSearchParams(r.URL.Query())
		if err != nil {
			writeProblem(w, r, http.StatusBadRequest, err.Error())
			return
		}

		result, err := books.Search(r.Context(), query, page)
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, newSearchResponse(&result, page))
		case errors.Is(err, catalog.ErrPageOutOfRange):
			writeProblem(w, r, http.StatusNotFound, fmt.Sprintf("page %d does not exist for this search", page))
		default:
			writeCatalogError(w, r, logger, err)
		}
	})
}

func parseSearchParams(values url.Values) (query string, page int, err error) {
	query = strings.Join(strings.Fields(values.Get("q")), " ")
	switch {
	case query == "":
		return "", 0, errors.New("query parameter q is required")
	case utf8.RuneCountInString(query) > maxSearchQueryLength:
		return "", 0, fmt.Errorf("query parameter q must be at most %d characters long", maxSearchQueryLength)
	}

	page = 1
	if raw := values.Get("page"); raw != "" {
		page, err = strconv.Atoi(raw)
		if err != nil || page < 1 {
			return "", 0, errors.New("query parameter page must be a positive integer")
		}
	}
	return query, page, nil
}

func newSearchResponse(result *catalog.SearchResult, page int) searchResponse {
	resp := searchResponse{
		Count:   result.Total,
		Page:    page,
		Results: make([]bookSummary, 0, len(result.Books)),
	}
	if result.HasNext {
		resp.NextPage = new(page + 1)
	}
	for i := range result.Books {
		resp.Results = append(resp.Results, newBookSummary(&result.Books[i]))
	}
	return resp
}

func newBookSummary(b *catalog.Book) bookSummary {
	authors := make([]string, 0, len(b.Authors))
	for _, a := range b.Authors {
		authors = append(authors, a.Name)
	}
	return bookSummary{
		ID:            b.ID,
		Title:         b.Title,
		Authors:       authors,
		Languages:     nonNil(b.Languages),
		CoverURL:      b.CoverURL,
		DownloadCount: b.DownloadCount,
	}
}

func writeCatalogError(w http.ResponseWriter, r *http.Request, logger *slog.Logger, err error) {
	if r.Context().Err() != nil {
		w.WriteHeader(statusClientClosedRequest)
		return
	}
	logger.ErrorContext(r.Context(), "book catalog request failed", slog.Any("error", err))
	if isTimeout(err) {
		writeProblem(w, r, http.StatusGatewayTimeout, "the book catalog did not answer in time, try again later")
		return
	}
	writeProblem(w, r, http.StatusBadGateway, "the book catalog is not available, try again later")
}

func isTimeout(err error) bool {
	var netErr net.Error
	return errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &netErr) && netErr.Timeout())
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
