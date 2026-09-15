# Design systems — 0.20.0 amendment: public catalogue reads and fork

Date: 2026-09-16
Status: approved by the user in chat. Amends
`2026-09-14-design-systems-design.md` and
`2026-09-15-design-systems-0.17-amendment.md`.

## Why

Since 0.17.0 the gallery at `GET /ds` lists every slug on the hub without a
credential. The "a slug is readable only with a credential, checked *before*
the lookup" rule was there to avoid an existence oracle for slugs — and the
gallery already publishes every slug, so the oracle it protected no longer
exists. Keeping the rule only makes the catalogue harder to read than the
gallery that already lists it.

The second half is what a reader does *after* they find a design system they
like: today they can read it, copy the bundle out by hand and re-register it.
That round trip should be one call, and the copy should record where it came
from.

## Decisions

### 1. Reading is public

- `GET /api/design-systems` and `GET /api/design-systems/{ref}` answer
  **without a credential**. With a valid credential they additionally compute
  `mine`; without one, `mine` is always `false`.
- `/ds/{slug}/…` reader routes no longer require a credential. The
  "401 before lookup" rule is retired.
- Slug reads therefore get exactly what id reads get: the CORS headers,
  `X-Hub-Version`, and `Cache-Control: no-cache`.
- `private, no-store` stays only on responses that were computed **with** a
  credential (the ones where `mine: true` is possible). Implementation rule:
  credentialed request → `private, no-store`; anonymous → `no-cache`.
- A meta-only (inert) record — a registration that died between its two
  Storage writes — stays 404 for everyone except its owner, unchanged.

### 2. Writing stays the owner's

`POST /api/design-systems/{ref}/versions`, `PUT /api/design-systems/{ref}`,
`DELETE /api/design-systems/{ref}/versions/{n}` and
`DELETE /api/design-systems/{ref}` are unchanged: owner only, and the two
deletes additionally go through the destructive-token policy. Versions remain
immutable; nothing about ownership changes.

### 3. Fork

`POST /api/design-systems/{ref}/fork`, body:

```json
{"slug": "<new slug>", "name": "…?", "description": "…?", "note": "…?", "version": 3}
```

- **Auth:** any valid Keboola credential. The caller does not have to own the
  source, and forking never touches the source.
- **What is copied:** the source's version `version` (default: its head)
  bundle, **verbatim**, as v1 of a NEW design system owned by the caller's
  project. The source's validation warnings are recopied as-is. The bundle is
  **not** re-validated against current limits: it was already normalised when
  it was stored, and the source proves it validated. Re-validating would let
  a limit lowered after the fact make an existing, readable design system
  unforkable.
- **Defaults:** `name` defaults to the source's name plus `" (fork)"`;
  `description` defaults to the source's description.
- **Provenance:** the new meta records
  `forked_from: {"id": …, "slug": …, "version": n}`, persisted in the meta
  JSON and tolerated when absent (every record written before 0.20.0). It is
  reported in every projection as `forked_from: {...} | null` and in the
  gallery JSON; the gallery card renders "forked from `<slug>`".
- **Limits:** the same rules as registration — 409 when the slug is taken,
  422 on an invalid slug or name, 429 on the per-project cap or the daily
  version budget, all under the same `_DS_CREATE_LOCK`. `count_owner` applies
  to the **forker**, not the source owner.
- **Response:** 201 with the new system's projection plus `"version": 1`.

## Documentation

`/context` (`endpoints`, `design_systems.fork`, updated
`design_systems.access` prose), `skills/artifact-publisher/SKILL.md`,
`agents/artifact-hub.md` ("make your own version: fork it — you own the copy,
the original stays the author's"), README (the two catalogue GET rows move to
the public table; the fork row joins the authenticated one; the Security
model paragraph is updated).

## Version

0.20.0 in `pyproject.toml`, `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `uv.lock`'s package line, and the CHANGELOG
head `## 0.20.0 — Take any design system, keep your own copy (2026-09-16)`.

## Tests

- anonymous `GET /api/design-systems` is 200 and every row has `mine: false`;
  the same call with a credential reports `mine` truthfully;
- anonymous `GET /ds/<slug>/bundle` is 200 and carries the CORS header;
- a meta-only record is 404 anonymously and for a non-owner, visible to its
  owner;
- fork happy path: new id, v1 bundle equals the source bundle, `forked_from`
  correct, owner is the forker, source unchanged and its owner unchanged;
- fork of a specific version; fork 409 on a taken slug; fork counts toward
  the forker's per-project cap; the forker cannot `PUT` or `POST` versions on
  the source (403);
- `/context` lists the fork route; docs strings; the existing tests that
  asserted 401 for anonymous slug reads are updated to the new contract.
