# Changelog

KBC Artifact Hub is one web address where you publish a document and
collaborate on it with your team, secured by the Keboola account you already
have.

## 0.15.1 — The agent trusts your configured token, not its memory (2026-09-09)

- When the stack refuses a token, the agent now re-reads `KBC_TOKEN` from the
  environment before it offers a sign-in or asks you for anything. A token it
  remembered from earlier in the conversation may have been rotated and is
  never the source of truth; the one you configured is. It also says plainly
  when a session predates the variable and needs a restart to see it, instead
  of starting a device sign-in nobody asked for. Same rule in the SKILL.md.

## 0.15.0 — Install the agent as a Claude Code plugin (2026-09-09)

- This repository is now a Claude Code marketplace. Two commands,
  `claude plugin marketplace add padak/kbc_ai_artifact` and
  `claude plugin install artifact-hub@kbc-artifact-hub`, install the
  `artifact-hub` subagent and the `artifact-publisher` skill into every
  project, and Claude Code keeps them current on its own after each release.
  The attested-release install path stays for anyone who wants provenance
  rather than convenience.
- The agent definition moved to `agents/artifact-hub.md`, where the plugin
  loader looks for it; `/agent` and the `AGENT.md` release asset are
  unchanged. The plugin's version is the project's version, and the release
  gate refuses a tag whose manifests lag behind.
- The landing page and the README now say how to install, and where to keep
  `HUB_URL`, `KBC_STACK` and `KBC_TOKEN` so an agent never needs a token
  pasted into a chat.

## 0.14.2 — The Keboola approval tab closes itself (2026-09-08)

- Signing in with the short code opened a Keboola tab that, once you
  approved, stayed open on the stack's raw JSON answer while this page had
  already moved on to the project picker. The sign-in page now closes that
  tab the moment the approval lands. When it cannot — you opened the
  approval through the link, or approved on another device — it says so
  above the project list instead of leaving you to guess.
- The approval tab still cannot reach or redirect the sign-in page: its
  link back is cut the instant it opens, which is what the old `noopener`
  flag did, minus the ability to close it.

## 0.14.1 — Owners read their own protected artifacts (2026-09-08)

- The admin studio could not open a password-protected artifact at all: the
  panel asked its owner for the reader password and had nowhere to type it.
  A read that carries a verified credential of the owning project now passes
  the password gate on every reader route, exports included. The password
  protects whoever holds the link; it was never a boundary against the owner,
  who could already remove it.
- Other projects, proposal authors and guest invitations still need the
  password or a valid unlock cookie, and the bypass sets no cookie, so a
  browser tab without auth headers still meets the unlock form.
- The owner check runs before the password path: a stale password header
  costs the owner nothing, records no failed attempt, and an exhausted
  password budget cannot lock the owner out.
- Writing a comment clears the same gate as reading, so the owner comments
  on their protected artifact without the password too; guests and other
  projects still need it.

## 0.14.0 — HEAD works, and /llms.txt is everywhere it should be (2026-09-02)

- Every public `GET` route now answers `HEAD` with the same status and
  headers and an empty body. Until now a `HEAD` was refused with 405, which
  meant `curl -I` and an assistant's header-only probe of a share link got
  nothing — the very clients the `Link` header exists for. A `HEAD` is never
  counted as a view.
- `/llms.txt` is now mentioned wherever an agent learns about this hub: the
  landing page's "for agents" card, section and footer, `SKILL.md` and
  `AGENT.md` (which also explain how to get from a share link to the hub's
  base URL). The API table on the landing page lists it too.

## 0.13.1 — The help links name all three documents (2026-09-02)

- The `<link rel="help">` relations on an artifact page and the `Link`
  response header on everything under `/a/` used to point at `/skill` alone.
  They now name all three documents an assistant might want, each with a
  title so a machine can tell them apart: `/llms.txt` first (what this hub
  is and how to read a share link), then `/skill` (a SKILL.md for any agent
  runtime) and `/agent` (a Claude Code subagent definition). The hidden note
  and `/llms.txt` already listed all three; the two header channels now agree
  with them.

## 0.13.0 — A shared link explains itself to your AI (2026-09-02)

- Forward an artifact link to an AI assistant and it now knows what it is
  looking at. Before, the assistant fetched the page, its text extraction
  dropped the embedded document, and it was left with a bare title: no hint
  that this was an Artifact Hub, where the readable document lived, or where
  the API was described. Every artifact page — and the password form in front
  of a protected one — now carries a note written for machines: what a share
  link is, where the raw HTML and the Markdown rendering are, which header a
  password goes in, and where `/context`, `/docs`, `/skill` and `/agent`
  are. It is hidden the way accessibility text is hidden, so a reader sees
  exactly the page they saw before, and the published document itself is
  untouched byte for byte.
- The same pointers travel as standard `<link rel>` relations in the page
  and as a `Link` response header on everything under `/a/`, so a client that
  never parses the body — `curl -I`, an agent's HEAD probe, a JSON 404 — gets
  them too.
- New `/llms.txt`, in the llmstxt.org convention: a short Markdown map of the
  hub that an assistant landing on an unfamiliar site checks first. It is
  listed in `/context` and links to everything above.

## 0.12.0 — Sign in with Keboola (2026-09-02)

- You no longer need to go and find a Storage API token to use this service.
  Open `/login`, pick your stack, approve the sign-in in your Keboola tab, and
  choose the project you are publishing as. The admin studio and the review
  page both accept that sign-in wherever they used to want a pasted token.
- **Keboola's own screen decides which projects a sign-in can reach**, and
  this service asks it to let you narrow that rather than granting everything
  by default. The project you pick here afterwards only says which of those
  you are publishing as.
- Two ways in, and the page picks the right one: a short code you approve in
  a browser — which works from anywhere, including a browser on a different
  device — or, when you are running this service on your own machine, a
  single browser hop with nothing to type.
- Signing out now ends the session on Keboola's side, rather than only
  forgetting it in this tab. A tab left open renews itself instead of asking
  you to sign in again every hour.
- Scripts and AI assistants can do the same thing: `/skill`, `/agent` and
  `/context` all describe the sign-in, and a session is used on the API
  exactly like a token was.
- Each credential goes where that kind of credential goes on Keboola itself:
  a Storage API token in `X-StorageApi-Token`, a sign-in in the standard
  `Authorization: Bearer` header. Mixing them up is answered by a message
  naming the right header, rather than a bare "unauthorized". Every curl
  example on the front page has a **token / sign-in** switch, so the whole
  page reads for whichever one you actually hold.
- Your credential still never reaches this service's storage or logs. It is
  relayed to Keboola, handed back to your own browser tab, and forgotten.

From the review of the above, before it shipped:

- **A hiccup while you are approving a code no longer wastes the code.** The
  short code is good for fifteen minutes, but any blip while the page waited
  for your approval — a dropped connection, a busy moment on Keboola's side —
  sent you back to the start with a new code and a new tab. The page now
  keeps waiting, easing off as it goes, and only gives up when the sign-in
  itself is actually refused or expires.
- **Signing out works even on a busy network.** Every visitor behind one
  office connection shares this service's hourly sign-in budget, and signing
  out used to spend from that same budget — so on a busy day it could quietly
  do nothing, leaving the session alive on Keboola while your tab had already
  forgotten it. It now has a budget of its own.
- **A project id left over in your environment no longer breaks a call made
  with a Storage API token.** The front page tells you to export one for a
  sign-in; a later call with an ordinary token of a different project was then
  refused, which is not what the documentation said would happen. A Storage
  token names its own project, and now really is the only thing that does.
- **The instructions an AI assistant reads were sending the sign-in to the
  wrong header** — the very mistake the service answers with an error naming
  the right one. The front page's copy-paste `hub` wrapper, `/skill` and
  `/agent` now all pick the right header for whichever credential you export.

## 0.11.0 — Second security review follow-up (2026-09-02)

**Two items need a change on your side: `HUB_TRUSTED_PROXY_CIDRS` (second
item) and, if you delete versions by script, the new 409 on the pinned
version (ninth item).**

- **Updates that change content and settings at once are now fail-closed.**
  A single `PUT /api/artifacts/{id}` can both narrow access (a reader
  password, closing submissions or comments, marking the document final) and
  widen it (clearing the password, reopening submissions, un-finalizing). The
  hub now commits the narrowing half *before* the new version and the
  widening half *after* it, so a Storage failure anywhere in the middle can
  never leave an artifact more open than it was. Previously a failed
  settings write could leave brand-new content publicly readable without the
  password the same request asked for. When such an update fails, the 502
  says exactly what is in force: nothing applied, the narrowing settings
  applied without the new version, or the new version live under the
  previous narrower settings with the widening changes still to resend.
  Successful updates are unchanged, including the response body.
- **A wrong password can no longer be retried without limit by pretending to
  come from somewhere else.** The hourly budget on failed password and
  invitation attempts was counted per client address, and the address was
  taken from a header the caller writes, so changing one line of the request
  bought a fresh budget. Forwarded addresses now count only when the
  connection came from a proxy network you named (`HUB_TRUSTED_PROXY_CIDRS`),
  the `X-Forwarded-For` chain is read from the right, skipping every trusted
  hop, every value has to parse as an IP address and is stored in canonical
  form, and at most `HUB_MAX_FORWARDED_CHAIN_ENTRIES` (16) entries are
  examined. Otherwise the real connection address is used, which nobody can
  choose. A second budget per document across all addresses
  (`HUB_MAX_UNLOCK_ATTEMPTS_PER_ARTIFACT_PER_HOUR`) backs it up. **If you run
  behind the Keboola proxy, set `HUB_TRUSTED_PROXY_CIDRS` to the network of
  every proxy hop in front of the hub.** Without it every reader shares one
  budget per document, which is safe but means one person guessing can push
  other readers into "too many attempts" for the rest of the hour.
- **An oversized request is now refused before it is read.** A large body
  sent to a comment, invitation or unlock endpoint used to be received and
  parsed in full before anyone checked whether the document even existed.
  The service now answers 413 up front, on both what the request claims to
  be sending and what it actually sends. Documents are unaffected: publishing
  and updating keep a ceiling derived from the existing size limits, and
  everything else accepts 256 KB by default (`HUB_MAX_SMALL_REQUEST_BYTES`).
  Comment and reply text also carry explicit length limits, reported by
  `GET /context`.
- **Made-up document links no longer leave anything behind.** Commenting on
  an address that names nothing used to reserve a small piece of memory that
  was never released, so repeated requests to invented links grew the
  service's footprint indefinitely. Addresses are now checked for shape and
  looked up before anything is reserved, and what is reserved is released
  and capped (`HUB_LOCK_REGISTRY_MAX_ENTRIES`).
- **Permanently erasing an artifact now removes its content files before the
  record that proves you own it, never the other way round.** Previously a
  Storage failure part-way through a purge could delete the ownership record
  first, and the retry the error asked for would answer "no such artifact",
  leaving a version file behind that nothing could reach or erase again. The
  retry now always works, including after a container restart, and finishes
  what the failed call started.
- **Git clones no longer follow HTTP redirects.** An allowed public git host
  could redirect the clone to an address the hostname check never saw; a
  redirect now fails the clone with a clear, credential-free error. The
  remaining resolver-to-connect DNS race for outbound git and webhook traffic
  is documented as an accepted residual risk with its operational
  mitigation, an egress policy in front of the container; see the README's
  "Network egress" section.
- **The hub now refuses to run twice.** Starting a second copy against the
  same cache directory fails immediately with an explanation instead of
  quietly losing the first copy's work. If two copies do end up running
  against the same Keboola project from different disks, whichever one
  notices stops writing counters and analytics, leaves the other's data
  untouched, and reports itself as not ready on `/health` so an operator can
  see it; the platform's own startup check keeps answering. This service has
  always been documented as exactly one process per organisation; now it
  enforces it.
- **Vault exports are bounded and streamed.** Building the Obsidian vault is
  the most expensive thing the service does for an unauthenticated caller,
  and it used to assemble the whole archive in memory. An artifact whose
  visible history and comments exceed `HUB_EXPORT_MAX_BYTES` (64 MB by
  default) is refused with 413 before anything is rendered, one client
  address may build `HUB_MAX_EXPORTS_PER_HOUR` vaults of one artifact per
  hour (20 by default) before the answer is 429, and the archive is written
  entry by entry into an owner-only temporary file that is removed as soon
  as the download finishes. The ZIP a caller receives is unchanged, byte for
  byte.
- **Deleting the version the head is pinned to is refused with 409** instead
  of silently succeeding. It used to leave the artifact pointing at a
  version that no longer existed while quietly serving a different one. Pin
  the head to another live version, or switch it back to `latest`, then
  delete.
- **Polling an unchanged artifact is now free.** `GET /a/{id}/live` answered
  304 for a matching `ETag`, but only after listing every version and every
  comment thread to work out what the tag should be. Each artifact now
  carries a revision number that every change bumps, the tag is derived from
  it, and an unchanged poll is answered from memory. The tag is
  process-local: after a restart an unchanged artifact hands out a new one
  and clients refresh once.
- **Webhook receiver keys can be rotated without changing the URL.**
  `POST /api/artifacts/{id}/webhooks/{receiver_id}/rotate-key` mints a fresh
  key; for `HUB_WEBHOOK_KEY_OVERLAP_S` (10 minutes by default) deliveries
  carry both the new and the previous signature so an unmigrated receiver
  keeps verifying. Responses that return keys are now served
  `Cache-Control: no-store`.
- **Destructive authority can be narrowed below "any token of the project".**
  `HUB_DESTRUCTIVE_TOKEN_POLICY` chooses who may trash, purge, rotate the
  link, delete versions or rotate webhook keys: `project` (the default and
  the previous behaviour), `admin` (a master token or a project admin's own
  token) or `allowlist` (`HUB_DESTRUCTIVE_TOKEN_IDS`). Publishing and every
  other owner action are unaffected. Shared projects should set `admin`.
- **The release pipeline no longer trusts anything it did not check itself.**
  Every third-party build step is pinned to an exact reviewed commit, a
  release refuses to publish from a commit that never reached `main`, and
  the tests, the compile check and the dependency audit all run again inside
  the job that signs the release. The production deployment example in the
  README pinned the previous release's tag; it is current again, and a test
  fails the build whenever the tag in that example, the package version and
  the changelog stop agreeing. Private-git publishing examples no longer
  place the git token in any process's command line.

## 0.10.0 — Security review follow-up (2026-09-01)

**If you run a webhook receiver, read the first item — it needs a change on
your side.**

- **Each webhook receiver is now signed with its own key.** Previously every
  receiver verified deliveries with one key shared across the whole service,
  so anyone running a single webhook could forge a delivery that looked
  genuine for any other receiver and any document. Keys are now bound to the
  document and the receiver URL. **Existing receivers will stop verifying:**
  read your new key from `GET /api/artifacts/{id}/webhooks` (owner only) and
  configure the receiver with it.
- Verifying a password now refuses a stored record that asks for an
  implausible amount of work, so a corrupted or tampered record can neither
  tie up the service nor quietly weaken the check.
- A comment thread's "resolved" mark and a guest invitation's "revoked" mark
  are now read strictly. A malformed record can no longer close a thread that
  is open, and an invitation whose revocation cannot be read is treated as
  revoked rather than still valid.
- Comparing two versions now requires the older one first. Asking for 5..3
  used to answer with additions and removals swapped under labels claiming
  the opposite; it is a clear error now.
- Pages no longer pass their address on to anywhere they link or load from,
  so a document's link cannot leak through a font request or an outbound
  click.
- The API reference now describes what the review page really does for a
  password-protected document — it serves the page and asks for the password
  in place, rather than the redirect it used to promise.
- Documentation now says plainly that pending proposals are subject to their
  own retention cap, instead of claiming they are kept forever.
- The deployed image no longer ships a networking library with five known
  vulnerabilities. Every release now fails automatically if a dependency with
  a published advisory reaches the production dependency list.
- A push notification is no longer sent to a destination the service could not
  check. A name that fails to resolve at delivery time now stops the delivery
  instead of being sent anyway, closing a way to redirect a notification to an
  internal address.
- Push notifications now carry an event id and a delivery id, so a receiver can
  tell a retry from something new and avoid acting twice.
- A receiver that stops answering can no longer make the service accumulate
  pending notifications without limit; past a configurable ceiling
  (`HUB_WEBHOOK_QUEUE_MAX`) the newest is dropped, and publishing is never held
  up waiting for one.
- An update that fails no longer leaves its settings behind. Changing a
  password, policy or status together with new content used to apply the
  settings first, so a failed upload answered with an error while the
  security-relevant half had already taken effect. Content lands first now
  and settings are written last, or not at all.
- A failed publish or update no longer strands a copy of the document in the
  author's own Keboola project. That copy could only ever be reached with the
  caller's token during the request, so it is removed right there when the
  version it was for fails to land; a publish that fails at its last step now
  cleans up its own record as well.
- A publish interrupted between its two writes used to leave an invisible,
  unreachable record behind forever. Startup now reaps such records once they
  are old enough that they cannot be a publish still in progress
  (`HUB_REAP_ABORTED_PUBLISH_AFTER_S`).
- Installing the AI agent definition is now a verified step. Each release
  publishes `AGENT.md`, `SKILL.md` and their checksums as attested assets, and
  the documented install downloads that copy, checks it and proves with
  `gh attestation verify` that this project's release process produced it —
  instead of copying whatever the live server happens to serve. `/agent` and
  `/skill` now report their own digest and version, and `/context` points at
  the matching release, so a server not running its own release can be
  spotted before anything is installed.
- Every documented API example now calls the service through a small shell
  function instead of passing the token as a command-line argument, where it
  was visible to anyone who could list processes on the machine for as long
  as the call ran.
- Two requests changing the same document at the same moment can no longer
  act on a state that has just stopped being true. Every change to a document
  now waits for the previous one to finish: two promotions of one proposal
  fire once, two deletions cannot remove the last live version between them,
  a document cannot be finalised underneath a submission already in flight,
  and nothing can land in a document while it is being erased. Comments are
  covered too, even though they address the document by its public link
  rather than its id.
- A deleted version's number is retired with it. Deleting the newest version
  used to hand its number to whatever was submitted next, so every link,
  comment and comparison that named it silently pointed at different content
  — including after a restart.
- The service now states its deployment shape plainly and watches for it
  being broken: one hub is one container, serving one organisation. Its index,
  locks and state snapshots are built for exactly one writer, so if a second
  instance ever writes state, the log says so in as many words instead of the
  two instances quietly overwriting each other.
- Authorization is documented as it was always meant: any token from the
  owning Keboola project has full owner authority, because the project is the
  team. The security review had flagged this as a gap; it is the design.
- Finalising a document now freezes its discussion too. Resolving, reopening
  and withdrawing a comment used to keep working on a document marked final or
  moved to the trash, so a "finished" record kept changing. The owner can still
  delete a comment thread — a comment that has to come off a finished document
  must stay removable.
- A comment thread now has a size ceiling of its own: 500 replies and 2 MB.
  Individual comments were capped, but nothing capped the thread they pile up
  in, and a thread is rewritten whole on every reply and re-read on every
  listing.
- A comment, a resolve or a guest-invitation revocation that is refused no
  longer shows up as if it had been accepted. A revocation that failed to save
  used to read as revoked on the server that handled it while the guest kept
  working everywhere else, and came back entirely after a restart.
- Publishing from git now says plainly that `git_ref` takes a branch or a tag.
  A commit id was documented as accepted but always failed inside git; it is
  now refused with a message that says to tag the commit instead.
- Permanently erasing a document can now actually be retried when it fails
  partway. It previously removed the document before its comments, so a
  failure left comments behind that nothing could reach or erase, and the
  retry the error asked for reported the document as already gone. Comments
  go first now, and the document stays until everything else is confirmed
  erased.

## 0.9.0 — Documents you can hand to another tool (this release) (2026-09-01)

- **Asking for a document in Markdown now actually gets you Markdown**,
  including when the author originally wrote it as a designed web page —
  tables, diagrams and code come through intact, and anything that can't
  survive the trip, like charts or images, is called out rather than
  silently dropped.
- **An author publishing a designed page can now attach the Markdown they
  wrote it from**, so a colleague or an AI assistant reading the document
  gets the author's own words instead of an automatic conversion of the
  finished page.
- **The downloadable history of a document now includes readable Markdown
  for every version**, not just the original files, so the full archive is
  usable outside a browser.
- The instructions we give AI assistants working with this service now tell
  them to always make a Markdown version available, so this is the default
  going forward rather than something you have to ask for.

## 0.8.0 — Documents update while you are reading them (2026-09-01)

- **Share a link, publish a revision, and everyone who already has the page
  open sees it appear** — no reload, no message telling people to refresh.
  This covers the document page, the review page, and the studio an owner
  uses to manage everything.
- Nothing is pulled out from under a reader: a document only swaps itself in
  if you're still at the top of it; scroll in, and you get a quiet notice
  instead. Anyone reading one specific version is never bumped off it, and a
  half-written comment always survives.
- Reviewers see each other's comments and new versions land in real time,
  and an owner reviewing proposals sees them appear without touching the
  page.
- Built to work everywhere reliably, including through office and hotel
  networks that quietly break flashier "live" approaches — and it costs
  almost nothing to run when nothing has actually changed.

## 0.7.4–0.7.5 — A password-protected document can now be reviewed by an outside guest (2026-09-01)

- An invited reviewer opening a password-protected document on their
  personal link used to hit a dead end — there was nowhere to type the
  password, the document wouldn't load, and a comment they'd written failed
  at the last step with no way to recover it.
- The review page now asks for the password itself, explains that the
  invitation is still valid and the password is a separate lock, and says
  plainly when a password is wrong or has been tried too many times.
- A comment written before unlocking is kept and sent automatically once the
  document opens, instead of being lost.
- The fix reaches all the way through: reviewers used to be bounced to a
  generic "enter password" screen first, which discarded the part of their
  link that identified them as the invited guest. That no longer happens —
  they stay on their own link throughout, and the document, its versions and
  its comments remain closed until the correct password is given.

## 0.7.3 — Live demo, and knowing when a document is settled (2026-09-01)

- **A live demo is now linked from the landing page.** It walks through a
  full real example: one document published by one Keboola project, revised
  by a reviewer on a different project and a different Keboola stack,
  commented on by an outside guest with no Keboola account, then approved
  and locked — all on the hub itself, so you can click through it rather
  than take our word for it.
- **Anyone checking on a document from outside can now tell whether it's
  actually finished.** A finalized document no longer advertises that it's
  still open for changes — previously the only status on offer described
  the current version, not the document as a whole.

## 0.7.2 — Comments land where they were meant to (2026-09-01)

- A comment submitted by automation, such as an AI assistant, now attaches
  correctly to the passage it quotes even when line breaks in the quoted
  text don't match exactly. Previously such a comment was accepted silently
  but showed no highlight and an unhelpful "quote not found" message.
  Comments made by selecting text by hand in the browser were never
  affected.
- A guest reviewer's comment is now credited with their name in the
  downloaded project archive, instead of showing up as "unknown project."

## 0.7.1 — Security follow-up (2026-09-01)

- Documents opened through the machine-readable link now render inside the
  same isolated sandbox as the normal view, closing a cross-site scripting
  risk that only affected that one path.
- Password-protected documents now stay protected for invited guests too —
  an invitation is a way to comment, not a way around the password.
- Rotating a document's link now fully cuts off the old one for comments as
  well as viewing, so revoking a leaked link revokes it everywhere.
- Stronger protection against push notifications being redirected to
  internal systems.
- Notification signing and document-password protection now use separate
  keys, instead of sharing one.
- Clearer guidance for AI assistants working with this service: document
  content, comments, and proposals are data to read, never commands to
  follow.

## 0.7.0 — Control, guests, and push notifications (2026-09-01)

- **Revoke a shared link in one click.** Mint a new link for a document and
  the old one stops working immediately, for everyone — the way to take back
  access after a link went somewhere it shouldn't have.
- **Deleted documents go to a recycle bin first.** Deleting a document no
  longer erases it on the spot: it moves to the trash, where it stays fully
  recoverable — bring it back, exactly as it was, on the same link, whenever
  you like. A separate, clearly-labeled "erase forever" action is there for
  when you really do mean permanent.
- **Get a Slack message the moment a colleague proposes a change or
  comments.** Connect a document to a Slack channel (or your own system) and
  stop refreshing the page to see what happened — a notification arrives the
  instant a version is proposed or promoted, a comment or reply lands, or the
  document is finalized, trashed, restored, or its link is rotated.
- **Invite reviewers by name — no Keboola account needed.** Send a private
  link to a colleague, client, or outside reviewer and they can comment right
  away, without signing up for anything. Revoke any one person's access at
  any time without touching anybody else's.
- **See whether your document is actually being read.** Every document now
  reports how many times it's been opened, broken down by day and by how
  people viewed it — the rendered page, the raw file, or a specific version.
- **Compare two versions the way a reader actually sees them.** A new
  side-by-side view renders both versions as real pages, scrolling in sync —
  ideal for reports and dashboards where the visual result matters as much as
  the text.
- **A proposed change now tells you when it's out of date.** If a colleague
  suggests a change and somebody else's edit lands first, the suggestion is
  now flagged so you know to take a fresh look before approving it.

## 0.6.0 — Security hardening

- An independent security review examined every part of the service; all
  forty findings it raised have been fixed.
- Documents now render inside an isolated sandbox, so a shared document can
  never reach the part of the page that holds your Keboola sign-in — even a
  document designed to try.

## 0.5.0 — Changelog and clearer guidance (2026-09-01)

- This page. A plain-English summary of what changed, so you don't have to
  read engineering release notes to know what's new.
- The step-by-step instructions we hand to AI assistants and technical teams
  now walk through full team workflows, not just individual actions — faster
  setup, fewer questions bounced back to you.

## 0.4.0 — Team collaboration ("project brain")

- Colleagues can highlight any passage in a document and leave a comment
  right there, with replies — a document becomes an ongoing discussion
  instead of a one-way memo.
- A dedicated review page lets anyone open the document in their browser,
  highlight text, and comment on it — no special software or training
  required.
- Download the complete history of a document — every version, who proposed
  it, every comment, and how each was resolved — as a self-contained,
  permanent record you can browse and search later.
- Decide exactly who is allowed to contribute changes or comments, from
  "anyone with the link" to a specific named list of collaborators.
- Mark a document "Final" once your team reaches agreement, locking it
  against further changes.

## 0.3.0 — Easier moderation and onboarding

- A new Admin page lets a document's owner approve or decline a colleague's
  suggested change with a single click — no technical steps required.
- New team members, and their AI assistants, can install a ready-made helper
  that already knows how to use the service, cutting setup time to minutes.
- The full API reference is now complete and kept accurate automatically,
  so any engineering team you bring in can integrate faster.

## 0.2.1 — Reliability fix

- Fixed an issue where links opened from behind the company network could
  point to an internal address instead of the public one. Links now always
  resolve correctly, wherever they're opened from.

## 0.2.0 — Document versions

- Every update to a document is saved as a new version, so nothing is ever
  lost — the full history is always available.
- Compare any two versions side by side to see exactly what changed.
- Colleagues can propose changes to a document; the owner reviews and
  approves them before they go live.
- Choose which version your readers see — always the newest one, or a
  specific version you've locked in.
- Publish documents directly from your team's private code repositories,
  fitting into the workflow you already use.

## 0.1.0 — Launch

- Publish any document — a web page, a formatted write-up, or content
  straight from a code repository — and get a unique, hard-to-guess public
  link to share.
- Add an optional password so only the people you've shared the link with
  can open the document.
- Your content is stored securely in your own Keboola account — you keep
  full ownership and control of it.
- Documents support rich formatting out of the box: diagrams, tables, and
  charts, with no extra setup.

---

Technical release notes: github.com/padak/kbc_ai_artifact/releases
