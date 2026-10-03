// Package catalog defines the books of the external catalog the service relies on.
package catalog

import "errors"

var (
	// ErrBookNotFound means that the catalog has no book with the requested ID.
	ErrBookNotFound = errors.New("book not found")
	// ErrPageOutOfRange means that a search has fewer result pages than requested.
	ErrPageOutOfRange = errors.New("search page out of range")
)

// Book is a book of the catalog.
type Book struct {
	ID            int
	Title         string
	Authors       []Person
	Subjects      []string
	Bookshelves   []string
	Languages     []string
	Summaries     []string
	CoverURL      string
	DownloadCount int
}

// Person is an author of a book.
type Person struct {
	Name      string
	BirthYear *int
	DeathYear *int
}

// SearchResult is one page of the books matching a search.
type SearchResult struct {
	Total   int
	Books   []Book
	HasNext bool
}
