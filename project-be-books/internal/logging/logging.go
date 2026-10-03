// Package logging builds the structured logger of the service.
package logging

import (
	"context"
	"io"
	"log/slog"
	"slices"
)

// New returns a JSON logger that writes records at or above level to w.
func New(w io.Writer, level slog.Leveler) *slog.Logger {
	return slog.New(contextHandler{
		Handler: slog.NewJSONHandler(w, &slog.HandlerOptions{Level: level}),
	})
}

type attrsKey struct{}

// WithAttrs returns a copy of ctx; records logged with it also carry attrs.
func WithAttrs(ctx context.Context, attrs ...slog.Attr) context.Context {
	existing, _ := ctx.Value(attrsKey{}).([]slog.Attr)
	return context.WithValue(ctx, attrsKey{}, append(slices.Clip(existing), attrs...))
}

type contextHandler struct {
	slog.Handler
}

func (h contextHandler) Handle(ctx context.Context, r slog.Record) error { //nolint:gocritic // slog.Handler signature
	if attrs, ok := ctx.Value(attrsKey{}).([]slog.Attr); ok {
		r.AddAttrs(attrs...)
	}
	return h.Handler.Handle(ctx, r)
}

func (h contextHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return contextHandler{Handler: h.Handler.WithAttrs(attrs)}
}

func (h contextHandler) WithGroup(name string) slog.Handler {
	return contextHandler{Handler: h.Handler.WithGroup(name)}
}
