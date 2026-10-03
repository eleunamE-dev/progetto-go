// Package logging builds the structured logger of the service.
//
// Request-scoped attributes, such as the request ID, travel in the
// context.Context: any record logged with that context carries them, without
// passing per-request loggers around.
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

// WithAttrs returns a copy of ctx carrying attrs: loggers created by New add
// them to every record logged with the returned context.
func WithAttrs(ctx context.Context, attrs ...slog.Attr) context.Context {
	existing, _ := ctx.Value(attrsKey{}).([]slog.Attr)
	return context.WithValue(ctx, attrsKey{}, append(slices.Clip(existing), attrs...))
}

// contextHandler decorates a slog.Handler with the attributes stored in the
// context of each record.
type contextHandler struct {
	slog.Handler
}

func (h contextHandler) Handle(ctx context.Context, r slog.Record) error { //nolint:gocritic // hugeParam: signature set by slog.Handler
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
