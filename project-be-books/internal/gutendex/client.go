// Package gutendex is a client for the Gutendex API (https://gutendex.com).
package gutendex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/eleunamE-dev/progetto-go/project-be-books/internal/catalog"
)

const (
	userAgent       = "bookreviews (+https://github.com/eleunamE-dev/progetto-go)"
	maxResponseSize = 10 << 20
)

var errNotFound = errors.New("not found")

// Client calls the Gutendex API.
type Client struct {
	baseURL    *url.URL
	httpClient *http.Client
}

// NewClient returns a client for the Gutendex instance at baseURL.
func NewClient(baseURL string, httpClient *http.Client) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("invalid Gutendex base URL %q", baseURL)
	}
	if u.Path == "" {
		u.Path = "/"
	}
	return &Client{baseURL: u, httpClient: httpClient}, nil
}

// Search returns a page of the books whose title or authors contain all the words of query.
func (c *Client) Search(ctx context.Context, query string, page int) (catalog.SearchResult, error) {
	params := url.Values{"search": {strings.ToLower(query)}}
	if page > 1 {
		params.Set("page", strconv.Itoa(page))
	}

	var list bookList
	err := c.get(ctx, "books/", params, &list)
	if errors.Is(err, errNotFound) {
		return catalog.SearchResult{}, catalog.ErrPageOutOfRange
	}
	if err != nil {
		return catalog.SearchResult{}, err
	}

	books := make([]catalog.Book, 0, len(list.Results))
	for i := range list.Results {
		books = append(books, list.Results[i].toCatalog())
	}
	return catalog.SearchResult{Total: list.Count, Books: books, HasNext: list.Next != nil}, nil
}

// Book returns the book with the given ID.
func (c *Client) Book(ctx context.Context, id int) (catalog.Book, error) {
	var b book
	err := c.get(ctx, "books/"+strconv.Itoa(id)+"/", nil, &b)
	if errors.Is(err, errNotFound) {
		return catalog.Book{}, catalog.ErrBookNotFound
	}
	if err != nil {
		return catalog.Book{}, err
	}
	return b.toCatalog(), nil
}

func (c *Client) get(ctx context.Context, path string, params url.Values, dst any) error {
	u := c.baseURL.JoinPath(path)
	u.RawQuery = params.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return fmt.Errorf("gutendex: build request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", userAgent)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("gutendex: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusNotFound:
		return errNotFound
	default:
		return fmt.Errorf("gutendex: GET %s: unexpected status %d", u.Path, resp.StatusCode)
	}

	if err := json.NewDecoder(io.LimitReader(resp.Body, maxResponseSize)).Decode(dst); err != nil {
		return fmt.Errorf("gutendex: decode %s: %w", u.Path, err)
	}
	return nil
}

type bookList struct {
	Count   int     `json:"count"`
	Next    *string `json:"next"`
	Results []book  `json:"results"`
}

type book struct {
	ID            int               `json:"id"`
	Title         string            `json:"title"`
	Authors       []person          `json:"authors"`
	Summaries     []string          `json:"summaries"`
	Subjects      []string          `json:"subjects"`
	Bookshelves   []string          `json:"bookshelves"`
	Languages     []string          `json:"languages"`
	Formats       map[string]string `json:"formats"`
	DownloadCount int               `json:"download_count"`
}

type person struct {
	Name      string `json:"name"`
	BirthYear *int   `json:"birth_year"`
	DeathYear *int   `json:"death_year"`
}

func (b *book) toCatalog() catalog.Book {
	authors := make([]catalog.Person, 0, len(b.Authors))
	for _, a := range b.Authors {
		authors = append(authors, catalog.Person(a))
	}
	return catalog.Book{
		ID:            b.ID,
		Title:         b.Title,
		Authors:       authors,
		Subjects:      b.Subjects,
		Bookshelves:   b.Bookshelves,
		Languages:     b.Languages,
		Summaries:     b.Summaries,
		CoverURL:      b.Formats["image/jpeg"],
		DownloadCount: b.DownloadCount,
	}
}
