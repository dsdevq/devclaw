# Codebase design — deep modules

Shared discipline and vocabulary for designing modules: a lot of behavior
behind a small interface, placed at a clean seam, testable through that
interface. Use when designing or restructuring code, deciding where a seam
goes, or making code more testable. The aim is leverage for callers, locality
for maintainers, and testability for everyone.

## Glossary — use these terms exactly

**Module** — anything with an interface and an implementation. Deliberately
scale-agnostic: a function, class, package, or tier-spanning slice.

**Interface** — everything a caller must know to use the module correctly:
the type signature, but also invariants, ordering constraints, error modes,
required configuration, and performance characteristics. Wider than "API" or
"signature".

**Implementation** — what's inside a module, its body of code.

**Depth** — leverage at the interface: the amount of behavior a caller (or
test) can exercise per unit of interface they have to learn. A module is
**deep** when a large amount of behavior sits behind a small interface,
**shallow** when the interface is nearly as complex as the implementation.

**Seam** — a place where you can alter behavior without editing in that
place; the *location* at which a module's interface lives. Where the seam
goes is its own design decision, distinct from what goes behind it.

**Adapter** — a concrete thing that satisfies an interface at a seam. Role,
not substance.

**Leverage** — what callers get from depth: one implementation pays back
across N call sites and M tests. **Locality** — what maintainers get: change,
bugs, and verification concentrate in one place. Fix once, fixed everywhere.

## Deep vs shallow

Deep = small interface + lots of implementation. Shallow = large interface +
thin pass-through implementation (avoid). When designing an interface, ask:
Can I reduce the number of methods? Simplify the parameters? Hide more
complexity inside?

## Principles

- **Depth is a property of the interface, not the implementation.** A deep
  module can be internally composed of small, swappable parts — they just
  aren't part of the interface. Internal seams (used by the module's own
  tests) are fine alongside the external seam.
- **The deletion test.** Imagine deleting the module. If complexity vanishes,
  it was a pass-through. If complexity reappears across N callers, it was
  earning its keep.
- **The interface is the test surface.** Callers and tests cross the same
  seam. If you want to test *past* the interface, the module is probably the
  wrong shape.
- **One adapter means a hypothetical seam; two adapters means a real one.**
  Don't introduce a seam unless something actually varies across it.

## Designing for testability

1. **Accept dependencies, don't create them.** `processOrder(order, gateway)`
   is testable; a function that constructs its own `StripeGateway` inside is
   not.
2. **Return results, don't produce side effects.**
   `calculateDiscount(cart): Discount` is testable; `applyDiscount(cart): void`
   that mutates in place is not.
3. **Small surface area.** Fewer methods = fewer tests needed; fewer params =
   simpler test setup.

---
*Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) (MIT © 2026 Matt Pocock).*
