# Web QA

- Drive the real UI with the project's runner (Playwright, Cypress, the project's e2e CLI). Locate by role and
  accessible name, not CSS structure; wait for the state the user waits for, not a fixed sleep.
- Act like the user: click, type, keyboard. Never trigger behavior by injecting script or calling handlers.
- Per changed flow, cover: the happy path, the empty state, the error state, and the boundary the spec names
  (limits, rounding, pagination edge).
- Run an automated accessibility scan (axe or the runner's) on each changed page as a supplementary check;
  it never replaces the flow checks.
- Visual checks compare against reviewed baselines only. A changed baseline is a review item, not a QA fix.
- A headless engine is not every browser. Name the browsers actually run; do not claim others.
