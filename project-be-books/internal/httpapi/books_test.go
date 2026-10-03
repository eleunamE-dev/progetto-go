package httpapi_test

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/catalog"
)

type fakeSearcher struct {
	result   catalog.SearchResult
	err      error
	calls    int
	gotQuery string
	gotPage  int
}

func (f *fakeSearcher) Search(_ context.Context, query string, page int) (catalog.SearchResult, error) {
	f.calls++
	f.gotQuery, f.gotPage = query, page
	return f.result, f.err
}

type timeoutError struct{}

func (timeoutError) Error() string   { return "i/o timeout" }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return false }

func searchRequest(target string) *http.Request {
	return httptest.NewRequest(http.MethodGet, target, nil)
}

func TestBookSearch(t *testing.T) {
	t.Parallel()

	t.Run("returns the matching books", func(t *testing.T) {
		t.Parallel()
		books := &fakeSearcher{result: catalog.SearchResult{
			Total:   231,
			HasNext: true,
			Books: []catalog.Book{
				{
					ID:            98,
					Title:         "A Tale of Two Cities",
					Authors:       []catalog.Person{{Name: "Dickens, Charles", BirthYear: new(1812)}},
					Languages:     []string{"en"},
					Subjects:      []string{"Historical fiction"},
					CoverURL:      "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg",
					DownloadCount: 50704,
				},
				{ID: 1, Title: "Untitled"},
			},
		}}
		srv, _ := newServer(t, books)

		rec := serve(srv, searchRequest("/book/search?q=dickens"))

		require.Equal(t, http.StatusOK, rec.Code)
		assert.Equal(t, "application/json", rec.Header().Get("Content-Type"))
		assert.JSONEq(t, `{
			"count": 231,
			"page": 1,
			"next_page": 2,
			"results": [
				{
					"id": 98,
					"title": "A Tale of Two Cities",
					"authors": ["Dickens, Charles"],
					"languages": ["en"],
					"cover_url": "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg",
					"download_count": 50704
				},
				{"id": 1, "title": "Untitled", "authors": [], "languages": [], "download_count": 0}
			]
		}`, rec.Body.String())
		assert.Equal(t, "dickens", books.gotQuery)
		assert.Equal(t, 1, books.gotPage)
	})

	t.Run("last page", func(t *testing.T) {
		t.Parallel()
		books := &fakeSearcher{result: catalog.SearchResult{Total: 70, Books: []catalog.Book{{ID: 1, Title: "Untitled"}}}}
		srv, _ := newServer(t, books)

		rec := serve(srv, searchRequest("/book/search?q=dickens&page=3"))

		require.Equal(t, http.StatusOK, rec.Code)
		assert.Equal(t, 3, books.gotPage)
		got := decode[map[string]any](t, rec)
		assert.InDelta(t, 3, got["page"], 0)
		assert.Nil(t, got["next_page"])
	})

	t.Run("no matches", func(t *testing.T) {
		t.Parallel()
		srv, _ := newServer(t, &fakeSearcher{})

		rec := serve(srv, searchRequest("/book/search?q=zzqxjvkwy"))

		require.Equal(t, http.StatusOK, rec.Code)
		assert.JSONEq(t, `{"count":0,"page":1,"next_page":null,"results":[]}`, rec.Body.String())
	})

	t.Run("collapses whitespace in the query", func(t *testing.T) {
		t.Parallel()
		books := &fakeSearcher{}
		srv, _ := newServer(t, books)

		serve(srv, searchRequest("/book/search?q="+url.QueryEscape("  Pride \t and   Prejudice ")))

		assert.Equal(t, "Pride and Prejudice", books.gotQuery)
	})

	t.Run("page out of range", func(t *testing.T) {
		t.Parallel()
		srv, _ := newServer(t, &fakeSearcher{err: catalog.ErrPageOutOfRange})

		rec := serve(srv, searchRequest("/book/search?q=dickens&page=9"))

		require.Equal(t, http.StatusNotFound, rec.Code)
		assert.Equal(t, "page 9 does not exist for this search", decode[problem](t, rec).Detail)
	})
}

func TestBookSearchRejectsInvalidParameters(t *testing.T) {
	t.Parallel()

	tests := map[string]struct {
		target     string
		wantDetail string
	}{
		"missing query":     {target: "/book/search", wantDetail: "query parameter q is required"},
		"blank query":       {target: "/book/search?q=%20%20", wantDetail: "query parameter q is required"},
		"query too long":    {target: "/book/search?q=" + strings.Repeat("a", 201), wantDetail: "query parameter q must be at most 200 characters long"},
		"page zero":         {target: "/book/search?q=dickens&page=0", wantDetail: "query parameter page must be a positive integer"},
		"negative page":     {target: "/book/search?q=dickens&page=-1", wantDetail: "query parameter page must be a positive integer"},
		"page not a number": {target: "/book/search?q=dickens&page=two", wantDetail: "query parameter page must be a positive integer"},
	}
	for name, tt := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			books := &fakeSearcher{}
			srv, _ := newServer(t, books)

			rec := serve(srv, searchRequest(tt.target))

			require.Equal(t, http.StatusBadRequest, rec.Code)
			assert.Equal(t, "application/problem+json", rec.Header().Get("Content-Type"))
			assert.Equal(t, tt.wantDetail, decode[problem](t, rec).Detail)
			assert.Zero(t, books.calls, "the catalog is not queried")
		})
	}
}

func TestBookSearchCatalogFailures(t *testing.T) {
	t.Parallel()

	tests := map[string]struct {
		err        error
		wantStatus int
	}{
		"catalog error":            {err: errors.New("gutendex: GET /books/: unexpected status 503"), wantStatus: http.StatusBadGateway},
		"catalog deadline":         {err: fmt.Errorf("gutendex: %w", context.DeadlineExceeded), wantStatus: http.StatusGatewayTimeout},
		"catalog network timeout":  {err: &url.Error{Op: "Get", URL: "https://gutendex.com/books/", Err: timeoutError{}}, wantStatus: http.StatusGatewayTimeout},
		"catalog connection error": {err: &url.Error{Op: "Get", URL: "https://gutendex.com/books/", Err: errors.New("connection refused")}, wantStatus: http.StatusBadGateway},
	}
	for name, tt := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, logs := newServer(t, &fakeSearcher{err: tt.err})

			rec := serve(srv, searchRequest("/book/search?q=dickens"))

			require.Equal(t, tt.wantStatus, rec.Code)
			assert.Equal(t, "application/problem+json", rec.Header().Get("Content-Type"))
			assert.NotContains(t, rec.Body.String(), "gutendex")
			record := findLog(t, logs, "book catalog request failed")
			assert.Equal(t, "ERROR", record["level"])
			assert.Equal(t, tt.err.Error(), record["error"])
		})
	}
}

func TestBookSearchClientGone(t *testing.T) {
	t.Parallel()
	srv, logs := newServer(t, &fakeSearcher{err: context.Canceled})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	rec := serve(srv, searchRequest("/book/search?q=dickens").WithContext(ctx))

	assert.Equal(t, 499, rec.Code)
	assert.Empty(t, rec.Body.String())
	assert.NotContains(t, logs.String(), "book catalog request failed")
}
