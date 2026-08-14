---
name: pr-hygiene
description: Rules for the pull request this bot opens - commit message shape, changelog entry, and scope limits. Read before opening or updating a PR.
---

# PR hygiene

- One logical change per PR. Do not bundle unrelated fixes.
- Commit subject: `<type>(<scope>): <imperative summary>`, 72 characters or fewer.
- Add the changelog entry under `## [Unreleased]` in the changed package's `CHANGELOG.md`.
- Link the driving issue in the PR body.
- Never modify generated files by hand; change the generator and regenerate.
