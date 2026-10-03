package gutendex_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/catalog"
	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/gutendex"
)

type receivedRequest struct {
	path      string
	query     url.Values
	accept    string
	userAgent string
}

func newClient(t *testing.T, status int, body string) (*gutendex.Client, *receivedRequest) {
	t.Helper()
	got := &receivedRequest{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.path = r.URL.Path
		got.query = r.URL.Query()
		got.accept = r.Header.Get("Accept")
		got.userAgent = r.Header.Get("User-Agent")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)

	client, err := gutendex.NewClient(srv.URL, srv.Client())
	require.NoError(t, err)
	return client, got
}

func fixture(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", name))
	require.NoError(t, err)
	return string(data)
}

func TestSearch(t *testing.T) {
	t.Parallel()

	t.Run("sends a normalized query and maps the results", func(t *testing.T) {
		t.Parallel()
		client, got := newClient(t, http.StatusOK, fixture(t, "search_first_page.json"))

		result, err := client.Search(context.Background(), "Charles DICKENS", 1)

		require.NoError(t, err)
		assert.Equal(t, "/books/", got.path)
		assert.Equal(t, url.Values{"search": {"charles dickens"}}, got.query)
		assert.Equal(t, "application/json", got.accept)
		assert.Contains(t, got.userAgent, "bookreviews")

		assert.Equal(t, 231, result.Total)
		assert.True(t, result.HasNext)
		require.Len(t, result.Books, 2)
		first := result.Books[0]
		assert.Equal(t, 98, first.ID)
		assert.Equal(t, "A Tale of Two Cities", first.Title)
		assert.Equal(t, []catalog.Person{{Name: "Dickens, Charles", BirthYear: new(1812), DeathYear: new(1870)}}, first.Authors)
		assert.Equal(t, []string{"en"}, first.Languages)
		assert.Equal(t, "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg", first.CoverURL)
		assert.Equal(t, 50704, first.DownloadCount)
		assert.Equal(t, 47530, result.Books[1].ID)
	})

	t.Run("requests later pages", func(t *testing.T) {
		t.Parallel()
		client, got := newClient(t, http.StatusOK, fixture(t, "search_first_page.json"))

		_, err := client.Search(context.Background(), "dickens", 3)

		require.NoError(t, err)
		assert.Equal(t, url.Values{"search": {"dickens"}, "page": {"3"}}, got.query)
	})

	t.Run("last page", func(t *testing.T) {
		t.Parallel()
		client, _ := newClient(t, http.StatusOK, fixture(t, "search_last_page.json"))

		result, err := client.Search(context.Background(), "pride prejudice", 1)

		require.NoError(t, err)
		assert.Equal(t, 6, result.Total)
		assert.False(t, result.HasNext)
		require.Len(t, result.Books, 2)
		assert.Equal(t, []catalog.Person{
			{Name: "Austen, Jane", BirthYear: new(1775), DeathYear: new(1817)},
			{Name: "MacKaye, Steele, Mrs.", BirthYear: new(1845), DeathYear: new(1924)},
		}, result.Books[1].Authors)
	})

	t.Run("no matches", func(t *testing.T) {
		t.Parallel()
		client, _ := newClient(t, http.StatusOK, `{"count":0,"next":null,"previous":null,"results":[]}`)

		result, err := client.Search(context.Background(), "zzqxjvkwy", 1)

		require.NoError(t, err)
		assert.Equal(t, catalog.SearchResult{Total: 0, Books: []catalog.Book{}, HasNext: false}, result)
	})

	t.Run("page out of range", func(t *testing.T) {
		t.Parallel()
		client, _ := newClient(t, http.StatusNotFound, `{"detail":"Invalid page."}`)

		_, err := client.Search(context.Background(), "pride prejudice", 2)

		require.ErrorIs(t, err, catalog.ErrPageOutOfRange)
	})
}

func TestBook(t *testing.T) {
	t.Parallel()

	t.Run("maps the book", func(t *testing.T) {
		t.Parallel()
		client, got := newClient(t, http.StatusOK, fixture(t, "book.json"))

		book, err := client.Book(context.Background(), 1342)

		require.NoError(t, err)
		assert.Equal(t, "/books/1342/", got.path)
		assert.Equal(t, catalog.Book{
			ID:      1342,
			Title:   "Pride and Prejudice",
			Authors: []catalog.Person{{Name: "Austen, Jane", BirthYear: new(1775), DeathYear: new(1817)}},
			Subjects: []string{
				"Courtship -- Fiction", "Domestic fiction", "England -- Fiction", "Love stories",
				"Sisters -- Fiction", "Social classes -- Fiction", "Young women -- Fiction",
			},
			Bookshelves: []string{
				"Best Books Ever Listings", "Category: British Literature", "Category: Classics of Literature",
				"Category: Novels", "Category: Romance", "Harvard Classics",
			},
			Languages:     []string{"en"},
			Summaries:     []string{`"Pride and Prejudice" by Jane Austen is a novel published in 1813.`},
			CoverURL:      "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
			DownloadCount: 190246,
		}, book)
	})

	t.Run("book without cover", func(t *testing.T) {
		t.Parallel()
		client, _ := newClient(t, http.StatusOK, `{"id":1,"title":"Untitled","authors":[],"formats":{"text/plain":"https://example.org/1.txt"}}`)

		book, err := client.Book(context.Background(), 1)

		require.NoError(t, err)
		assert.Empty(t, book.CoverURL)
		assert.Empty(t, book.Authors)
	})

	t.Run("unknown book", func(t *testing.T) {
		t.Parallel()
		client, _ := newClient(t, http.StatusNotFound, `{"detail":"No Book matches the given query."}`)

		_, err := client.Book(context.Background(), 999999999)

		require.ErrorIs(t, err, catalog.ErrBookNotFound)
	})
}

func TestUpstreamFailures(t *testing.T) {
	t.Parallel()

	tests := map[string]struct {
		status  int
		body    string
		wantErr string
	}{
		"server error":   {status: http.StatusServiceUnavailable, body: "upstream down", wantErr: "unexpected status 503"},
		"malformed body": {status: http.StatusOK, body: `{"id":`, wantErr: "decode /books/1342/"},
	}
	for name, tt := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			client, _ := newClient(t, tt.status, tt.body)

			_, err := client.Book(context.Background(), 1342)

			require.ErrorContains(t, err, tt.wantErr)
			assert.NotErrorIs(t, err, catalog.ErrBookNotFound)
		})
	}
}

func TestRequestsHonorTheContext(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	t.Cleanup(srv.Close)
	client, err := gutendex.NewClient(srv.URL, srv.Client())
	require.NoError(t, err)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err = client.Book(ctx, 1342)

	require.ErrorIs(t, err, context.DeadlineExceeded)
}

func TestNewClientRejectsInvalidBaseURL(t *testing.T) {
	t.Parallel()

	for _, baseURL := range []string{"", "gutendex.com", "https://", "://gutendex.com"} {
		_, err := gutendex.NewClient(baseURL, http.DefaultClient)
		assert.Error(t, err, "base URL %q", baseURL)
	}
}

func TestLiveGutendex(t *testing.T) {
	if os.Getenv("GUTENDEX_LIVE_TEST") == "" {
		t.Skip("set GUTENDEX_LIVE_TEST=1 to run against https://gutendex.com")
	}
	t.Parallel()
	client, err := gutendex.NewClient("https://gutendex.com", &http.Client{Timeout: 2 * time.Minute})
	require.NoError(t, err)

	book, err := client.Book(t.Context(), 1342)
	require.NoError(t, err)
	assert.Equal(t, "Pride and Prejudice", book.Title)
	assert.NotEmpty(t, book.CoverURL)

	result, err := client.Search(t.Context(), "pride prejudice", 1)
	require.NoError(t, err)
	var ids []int
	for _, b := range result.Books {
		ids = append(ids, b.ID)
	}
	assert.Contains(t, ids, 1342)

	_, err = client.Book(t.Context(), 999999999)
	require.ErrorIs(t, err, catalog.ErrBookNotFound)
}
