# Frontend design

Applies only when your change touches a UI — a page, component, form, or
styling. Skip it entirely for backend, CLI, or data work.

## Honor the project's design system FIRST

Before any choice below, find and obey the project's own system: design notes in
AGENTS.md / CLAUDE.md, a tokens or theme file (`theme.*`, `tokens.*`, the
Tailwind config, existing CSS custom properties), and how the current components
already look. Apply it; never override it. Everything below is for greenfield UI
or where no system exists — it never trumps an established one.

## Distinctive, not templated

Aim for a deliberate look you could defend in a review. Avoid the tells of
default AI-generated UI:

- warm-cream + terracotta + serif; a purple/blue gradient hero on white
- Inter / Roboto / system-font picked as the "safe" face; emoji as section markers
- everything centered; `rounded-lg` on everything; the accent-bar-on-a-rounded-card motif

## The choices that carry it

- **Type:** a deliberate pairing (not one system font), a real modular scale, body measure ~65ch.
- **Color:** intentional neutrals (not default mid-grey) + one confident accent. Keep semantic colors (good / warn / critical) separate from the accent so state reads clearly.
- **Both themes:** design light AND dark from the start, driven by tokens (CSS custom properties), not a naive `invert()`.
- **Layout does the spacing:** flex/grid with `gap`, responsive; wide content (tables, code) scrolls inside its own container, never the page body.
- **Interaction:** interactive things look interactive; keyboard focus is visible; contrast meets WCAG AA; honor `prefers-reduced-motion`.
- **Content:** real, plausible copy and data — never lorem ipsum.
