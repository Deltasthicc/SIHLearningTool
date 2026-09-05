# PRISM

**P**ersonalized **R**eadiness **I**ntelligence & **S**kill **M**apping

An explainable, cross-domain skill-intelligence platform built for **SIH26101** (MoSPI): it
builds an experience-level-aware prototype competency profile, runs an explainable gap analysis against a target level,
orders a prerequisite-aware learning pathway, produces provider-labelled recommendations, turns
uploaded material into source-grounded quizzes, and closes the loop with adaptive practice
evidence. It's built on top of an adaptive DSA-learning RPG engine — that game is still here as
optional "Quest mode," but only its DSA browser path is currently verified end-to-end.

> **This is a prototype, honestly labeled as one.** iGOT Karmayogi and NSSTA/TPAC are
> `catalog-fallback` integrations today, not live enrolment/progress syncs. Authenticated iGOT
> interfaces exist, but this project has no approved contract, credentials, or sandbox and never
> fabricates access. Current browser/product routes still use the username/player-ID demo flow and
> do not invoke the implemented OIDC/RBAC foundation, so they are not protected routes. Lane 2 now
> provides cross-reviewed JWT verification, issuer/subject identity binding, RBAC, audit and
> deployment-database tenant primitives; that is not a browser login, approved government IdP or
> permission to process real personnel data. See
> [`docs/contracts/identity-authorization.md`](docs/contracts/identity-authorization.md),
> [`docs/contracts/data-authorization.md`](docs/contracts/data-authorization.md), and
> [`SIH26101_MASTER_CHECKLIST.md`](SIH26101_MASTER_CHECKLIST.md) for the exact boundary;
> [`SIH26101_TEAM_ORCHESTRATION.md`](SIH26101_TEAM_ORCHESTRATION.md) owns the execution model.

The complete user-supplied build scope is preserved as `PS-01`…`PS-18` in
[`docs/SIH26101_PROBLEM_STATEMENT.md`](docs/SIH26101_PROBLEM_STATEMENT.md). Read it before scoping
features; the current demo implements only a subset.

## What's real right now (verified in this repo, not just claimed)

Everything below is evidence recorded through 1 September 2026: live test runs, application boots,
HTTP calls and focused PostgreSQL/Keycloak drills. It is not inferred from configuration or copied
from a proposal.

- **4 domains, 1 engine.** `backend/services/curricula.py` holds DSA Fundamentals, Official
  Statistics & Data Governance, Public Policy & Programme Evaluation, and Digital & AI Literacy —
  34 competencies total, each with a stable ID, prerequisites, and a 1–5 target level, validated
  for cycles and dangling references at import time. Every domain is materialized as a backend dungeon:
  `db/seed.py`'s `seed_curricula_dungeons()` materializes rooms + a namespaced boss
  (`boss::official-statistics`, not a shared `"boss"` string) for the three new domains alongside
  DSA's existing seeding. The Academy-to-Quest browser route is currently broken for the three
  non-DSA domains; the master checklist records the exact repair.
- **A working competency-gap engine.** `backend/services/learning_engine.py` blends demonstrated
  quest evidence (65%) with self-assessment (35%) when both exist, explains its evidence source in
  plain text, caps the pathway target by experience level, and orders the resulting gaps
  prerequisite-first — all deterministic, no black box.
- **Source-checked quiz generation with a deterministic fallback.** `backend/services/quiz_generator.py`
  validates every Gemini-drafted question against the source text (exactly four unique options, a
  valid answer index, and a `source_excerpt` that must match the uploaded material after
  whitespace/case normalization); anything that fails validation, or any request with no API key configured, falls back
  to a deterministic extractive generator instead of failing or fabricating.
- **Bounded file ingestion.** `backend/services/content_ingestion.py` extracts text from TXT, MD,
  PDF, and DOCX uploads with a 5&nbsp;MB cap, a 100-page PDF cap, and a DOCX zip-expansion guard —
  and only ever persists a hash, character count, and short excerpt, never the original file or
  full extracted text.
- **An honest integration boundary.** `backend/services/learning_catalog.py` never returns a
  fabricated course ID or enrolment record. Every recommendation is either a link to this app's own
  adaptive practice quest or a link to iGOT/NSSTA's public catalog pages, tagged
  `catalog-fallback`. Environment variables currently change the displayed status but do not
  constitute a real adapter or successful authenticated health check. Some non-DSA internal links
  also depend on the P0 routing repair; both limitations are tracked in the master checklist.
- **A cross-domain room-unlock fix.** The original DSA-only unlock check
  (`routes/game.py::_is_room_unlocked_for_player`) only understood the hardcoded DSA topic graph;
  any non-DSA room would have been permanently locked. It now falls back to
  `services/curricula.py` for any topic outside that graph, so the new domains are actually
  playable, not just seeded.
- **The Academy UI.** `frontend/app/academy` (backing component: `components/AcademyHub.jsx`) is a
  full profile form, a per-domain diagnostic, an explainable pathway view, and the quiz
  upload/preview flow, built on the existing Pixel design-system primitives.
- **An honest admin view.** `frontend/app/admin` shows aggregate-only organizational gap data (no
  individual learner record, ever) with an explicit banner stating there is no RBAC boundary yet.
- **341/341 backend tests pass on the current Lane 2 branch (347/347 with the local PostgreSQL
  Compose service reachable).** The latest full gate covers the earlier cross-domain engine and
  ingestion behavior plus database migrations, governance records, OIDC verification, identity
  binding/RBAC, bootstrap invariants, data rights (including accurate deletion-count reporting under
  concurrent writes), retention (including atomic PostgreSQL row-claiming for concurrent enforcement
  runs, live-verified against a real trigger-protected table with genuinely forced concurrent
  candidate-selection overlap and a negative control) and adversarial backup/restore cases. The 6
  additional PostgreSQL-only tests skip cleanly (not fail) without Docker running. Reported warnings
  are not failed tests; the exact warning count/type varies by run (SQLite datetime-adapter
  deprecations from two regression tests, and possibly `.pytest_cache` write-contention warnings
  when both agents run the suite concurrently in the same shared working tree) — re-run and read the
  actual warning text before citing a specific count.
- **Lane 2's core platform foundation is implemented and reciprocally reviewed.**
  PostgreSQL 16 migrations, migration-gated startup, local Keycloak OIDC verification, identity
  binding, fixed RBAC policy, audit/data-rights primitives, local backup/restore drills, the
  atomic-row-claiming retention-enforcement job (a live-tested fix for a real PostgreSQL
  concurrency defect Codex found), and a PostgreSQL trigger on `audit_events` are all
  **Codex-accepted** on independent immutable review. The trigger blocks `UPDATE` only, not
  `DELETE`: an earlier version blocked both and Codex's cold audit caught that this made the
  retention job unable to ever delete its only registered category once a maximum retention is
  cited, so a follow-up migration (Package V) retired the `DELETE` rejection — accepted by Codex as
  correct production behavior — while the genuine append-only guarantee (rows are never removed
  except through the retention job) remains an application-layer property, never a database one.
  Package V's own test/evidence hardening (closing two bounded findings from that same Codex
  review — a disposable-database cleanup gap and a concurrency test needing genuinely forced
  overlap) has also passed Codex's narrow immutable re-review at `ac5a2e7`; the local Lane 2
  foundation is accepted with no remaining correctness finding. Existing HTTP routes do not
  compose any of these primitives yet; see the handoff below.
- **`npm audit --omit=dev` reports 0 vulnerabilities** after bumping the `next`/postcss dependency
  override that was pinning an old vulnerable version.

## What's aim, not yet reality

- **No live iGOT or NSSTA integration.** Public KB-iGOT engineering documentation demonstrates
  authenticated internal interfaces, but no open partner onboarding contract, credentials, or
  sandbox is configured here; no NSSTA API was verified. Production activation needs approved
  access and a real adapter behind the interface already sketched in
  `learning_catalog.py`.
- **No protected product route or browser SSO yet.** The Lane 2 resource-server foundation verifies
  OIDC JWTs and resolves server-side principals through active bindings and RBAC, but Lane 5 has not
  attached it to `backend/routes/**`, and Lane 1/5 have not built Authorization Code + PKCE login,
  session/logout or an approved government IdP integration.
- **No official competency ownership.** The four curricula are a reviewed, internally-consistent
  *demonstration* taxonomy, not an MoSPI/NSSTA/CBC-approved role-to-competency map. A real pilot
  needs a named domain owner to sign off on targets and descriptors.
- **Two database profiles, not production operations.** SQLite remains the documented zero-setup
  local-demo profile, including its compatibility `ensure_columns()` path. PostgreSQL 16 support is
  additive and migration-managed through Alembic; PostgreSQL startup refuses a stale revision.
  Live forward/backward migration and restore drills passed, but scheduled encrypted/offsite
  backups, key ownership and an operational DR runbook remain open.
- **No content-review workflow.** Generated quiz questions are demo-ready, not publish-ready — there
  is no draft/review/approve/retire state yet, so nothing stops an unreviewed item from being
  served.
- **Several explicit problem-statement capabilities are still absent.** There is no learner
  assistant, PPTX/video transcript pipeline, virtual lab, real multilingual journey, learning-hours
  evidence, predictive workforce model, browser SSO, malware scanning, background queue, or
  observability.
  These are mapped to owners and honest demo/pilot boundaries in the requirement contract.

## Lane 2 completion and handoff

Packages A–N and Q on `codex/lane-2-core-data/bootstrap` are implemented and reciprocally reviewed;
Package P/S adds retention/key-rotation evidence, including a live-tested atomic PostgreSQL
concurrency fix, and a deliberately unwired authenticated-encryption envelope; Package T is a full
independent security/data audit that found and fixed two further real issues; Package U reviewed a
second external audit's four proposed database-hardening items, rejected three with technical
reasoning (PostgreSQL row-level security and a legacy SQLite-to-PostgreSQL data migration are
deferred because there is no identified real source dataset, continuity requirement, approved
field/identity mapping, conflict policy, reconciliation contract or acceptance owner — not because
of any tenant model, which is irrelevant to whether that migration could exist; a self-stored
evidence hash and a "logs lost on crash" justification for full audit triggers don't hold up
either), and implemented the one correctly-scoped item that did — a PostgreSQL trigger on
`audit_events`. Codex's own cold immutable audit accepted Package S and Package T outright, but
found that Package U's original trigger (which rejected both `UPDATE` and `DELETE`) directly
conflicted with the retention job, whose only registered category is `audit_events`: once any
maximum retention is cited, the job's own `DELETE` would fail against its own project's trigger.
Package V fixed this with a follow-up migration that retires only the `DELETE` rejection — the
database now blocks `UPDATE` only, and the genuine append-only guarantee (rows are never removed
except through the retention job under a cited maximum) is an application-layer property, never a
database one. Codex's subsequent review accepted Package V's production migration/retention
behavior outright and raised two bounded test/evidence-hardening findings (a disposable-database
cleanup gap and a concurrency test needing genuinely forced overlap plus a negative control), both
of which are now closed — see `LANE2_SYNC.md` for the full audit trail and evidence. Package W on
`main` adds deterministic, read-only repository functions for current role targets, latest typed
evidence, latest assessments and latest source versions; a privacy-safe database-status command;
and an exact integration guide for every consuming lane. Its final legacy-column count repair at
`8d0d1de` awaits Claude's immutable review. The current full backend gate is **442 passed, 6
skipped** with PostgreSQL stopped and **448 passed** with the local Compose PostgreSQL healthy.
This completes the current hackathon Lane 2 foundation; it does **not** make the whole application
production-ready.

- **Lane 5 — Product API/Integrations:** attach Bearer verification, binding, permission,
  deployment-tenant and object-scope checks to every protected route; stop treating request
  `player_id` as authority; add 401/403 and negative API tests; expose the latest-assessment query;
  protect any identity-binding, export/deletion and audit-read APIs; implement API rate limiting
  against Lane 6's agreed policy/evidence criteria.
- **Lanes 1 + 5 — Browser identity:** implement Authorization Code + PKCE, exact redirect/state/
  nonce handling, session/logout and error/recovery UX against the selected IdP.
- **Lane 6 — Quality/Release:** run integration CI at merge head; define and verify threat-model,
  scanning, rate-limit, telemetry and secrets requirements while coordinating code changes with
  the owning lane; own scheduled encrypted/offsite backups, restore runbooks and
  DR exercises; keep public operational documentation current.
- **Accountable external owners:** approve the production/government IdP and claims, authoritative
  organization/department/cohort model, retention/legal basis, key ownership, privacy/security
  assessment and go-live. One database is one tenant today; row-level multi-organization isolation
  is not implemented.

The exact database/repository usage rules and copy-ready assignments for all five consuming lanes
are maintained in [`LANE2_INTEGRATION_GUIDE.md`](LANE2_INTEGRATION_GUIDE.md). The broader delivery
assignments remain in
[`SIH26101_TEAM_ORCHESTRATION.md`](SIH26101_TEAM_ORCHESTRATION.md#lane-2-completion-and-cross-lane-handoff).

The current itemized backlog is [`SIH26101_MASTER_CHECKLIST.md`](SIH26101_MASTER_CHECKLIST.md),
the strategy is [`SIH26101_WINNING_PLAYBOOK.md`](SIH26101_WINNING_PLAYBOOK.md), and the six-lane
delivery model is [`SIH26101_TEAM_ORCHESTRATION.md`](SIH26101_TEAM_ORCHESTRATION.md).

## How it plays

1. **Create a character** — the current browser route remains username-only (see
   [Player identity](#player-identity)); the Lane 2 identity foundation is not wired into this flow.
2. **Open the Academy** (`/academy`) — build a profile from designation, department, assignment,
   qualifications, experience, prior training, preferred language, and career goal.
3. **Diagnose gaps** — rate yourself 0–5 per competency; the engine blends that with real quest
   evidence where it exists and explains exactly how.
4. **Follow the pathway** — a prerequisite-ordered list of what to close first, each step showing
   its observed level, target, gap, and priority.
5. **Practice** — the DSA Quest path works today; the non-DSA Academy-to-Quest browser paths are a
   known P0 repair even though their backend dungeons are seeded.
6. **Answer in free text** — an AI grader judges meaning, not exact wording: correct / partial /
   incorrect.
7. **Generate a source-checked quiz** — upload TXT, Markdown, PDF, or DOCX; every accepted question
   cites a passage matched after whitespace/case normalization.
8. **Check the org view** (`/admin`) — aggregate, PII-free gap demand across every learner.

## Architecture

```text
PRISM/
|-- frontend/   Next.js website — no direct AI or DB access
|-- backend/    FastAPI server — API, SQLite demo/PostgreSQL profile, migrations, security + learning rules
`-- services/   Optional standalone AI engine (real sentence-embedding grading, not used by default)
```

The frontend only ever calls the backend's REST API (`frontend/lib/api/client.js` is the one file
that calls `fetch`, including the `learning` export added for this project). The backend is the
single source of truth for game and skill-intelligence state and the only thing that touches the
database or an LLM.

## Run it locally

**Backend** (start first):

```powershell
cd backend
if (-not (Test-Path .venv\Scripts\python.exe)) { py -3.12 -m venv .venv }
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m uvicorn main:app --reload --port 8000
```

Set `GEMINI_API_KEY` in `backend/.env` (get one at [Google AI Studio](https://aistudio.google.com/apikey))
to enable live question/quiz generation and semantic grading — every AI-dependent path has a
deterministic fallback if you skip this. Runs at `http://localhost:8000`, auto-seeds all four
dungeons plus a demo DSA player on first launch. Docs at `http://localhost:8000/docs`.

**Frontend:**

```powershell
cd frontend
npm install
if (-not (Test-Path .env.local)) { Copy-Item .env.local.example .env.local }
npm run dev
```

Runs at `http://localhost:3000`. Without the backend running, every action fails with
"Could not reach the backend."

### Player identity

The current create/login browser flow is username-only with no password. Do not reuse a real
password as your username. The backend now contains a tested OIDC verifier, identity-binding and
RBAC foundation, but the product routes do not call it. Lane 1/5 must complete the protected login
and route handoff before any non-synthetic learner or organizational data touches this app.

## Competency graph & data model

`backend/services/curricula.py` is the data-driven catalog for the three new domains; every
competency has a globally unique ID, label, description, prerequisites, and target level.
`backend/services/knowledge_graph.py`'s `TOPIC_GRAPH` remains the DSA domain's own graph — kept
separate deliberately rather than folded into `curricula.py` too, since DSA's seeding, tests, and
frontend sprite/label mappings all still key off it directly (see
`docs/archive/SIH26101_FEASIBILITY_AND_ROADMAP.md` §2.3 for why unifying the two is a follow-up, not done
here).

| Competency (Official Statistics & Data Governance) | Prerequisites |
|---|---|
| `os_statistical_foundations` | — |
| `os_data_collection` | `os_statistical_foundations` |
| `os_visualization` | `os_statistical_foundations` |
| `os_data_quality` | `os_data_collection` |
| `os_sampling_design` | `os_statistical_foundations`, `os_data_collection` |
| `os_gis` | `os_data_quality`, `os_visualization` |
| `os_big_data` | `os_data_quality` |
| `os_official_statistics` | `os_sampling_design`, `os_data_quality` |
| `os_ml` | `os_official_statistics`, `os_big_data` |

(this is a DAG, not a tree — several competencies have more than one prerequisite; `curricula.py`
is the authoritative source, and Public Policy / Digital Literacy follow the same shape)

A competency unlocks once every prerequisite is demonstrated or its room is cleared (checked
live, cross-domain, in `routes/game.py::_is_room_unlocked_for_player`); each domain's mastery
challenge unlocks once every one of its competencies is proven.

State lives in the configured SQLite demo database or PostgreSQL database: `players`,
`learner_profiles`, `competency_assessments`,
`learning_materials`, `generated_quizzes`, `accuracy_history` (rolling last-5 accuracy per player
per topic), `dungeons` (now `curriculum_slug`-tagged) / `rooms`, `questions`, `answer_submissions`,
`guilds`, `game_sessions`, `role_targets`, `evidence_records`, `source_versions`, `audit_events` and
`identity_bindings`. SQLite is the zero-setup local demo. PostgreSQL uses Alembic migrations and
migration-gated startup; neither profile alone is evidence of production operations.

## AI logic

| System | How it works |
|---|---|
| Question generation | Gemini prompt → question + expected answer + hint, per topic/difficulty. Always fresh, never a static bank. |
| Answer grading | Gemini judges semantic correctness of free text against the expected answer → `correct`/`partial`/`incorrect`. |
| Difficulty tuning | Epsilon-greedy bandit: 90% picks by recent accuracy (>80%→hard, >50%→medium, else easy), 10% random exploration. Boss fights are always hard. |
| Topic routing | Recommends the weakest currently-unlocked topic. |
| Competency-gap analysis | Deterministic 0–5 blend (65% demonstrated / 35% self-assessed when both exist), explicit evidence text, experience-capped target, priority tiers. |
| Learning pathway | Topological (prerequisites-first) ordering of gaps by severity, with a recommended next action per step. |
| Quiz generation | Gemini draft constrained to the uploaded document, validated against a strict source-grounding schema; deterministic extractive fallback if unavailable or invalid. |

**Fallbacks** (no AI-dependent step can hard-fail): question generation retries Gemini 3× with
backoff, then falls back to a templated question. Grading falls back to word-overlap scoring. Quiz
generation falls back to a deterministic extractive generator. Difficulty and topic routing never
call an LLM at all.

## API reference

Full request/response schemas are served live at `http://localhost:8000/docs`.

| Endpoint | Purpose |
|---|---|
| `POST /game/player/create` | Create a player |
| `GET /game/player/{id}` | Fetch stats + accuracy history (own-player auth required) |
| `GET /game/player/by-username/{username}` | Resolve a username to `{player_id, username}` only — the unauthenticated demo-login bootstrap step; never returns profile data |
| `GET /game/dungeons` / `dungeon/{id}` | List / fetch a dungeon and its rooms (list now includes `slug`) |
| `POST /game/session/start` | Start a run, bump login streak, open first room |
| `POST /game/room/enter` | Enter a room — picks difficulty, generates the question |
| `POST /game/answer/submit` | **Core loop**: grade, apply XP/damage, update accuracy, check room/dungeon completion |
| `POST /game/hint/use` | Spend a hint token |
| `GET /game/dungeon/{id}/next-topic` | Recommend next room |
| `POST /game/guild/create` / `join` · raid endpoints | Guild + raid lifecycle |
| `GET /game/leaderboard` / `leaderboard/guild` | XP rankings |
| `POST /ai/question/generate` · `/ai/answer/judge` · `/ai/difficulty/next` · `/ai/graph/next-topic` | Direct access to each AI subsystem |
| `GET /ai/dashboard/{player_id}` | Full AI Core dashboard payload |
| `GET /learning/curricula` | All 4 domains and their competencies |
| `GET/PUT /learning/profile/{player_id}` | Read or upsert a learner's competency profile |
| `POST /learning/assessment/{player_id}` | Run an explainable gap analysis + pathway |
| `GET /learning/pathway/{player_id}?curriculum_slug=...` | Recompute the latest pathway |
| `POST /learning/quiz/generate` (multipart) | Upload a document, generate a grounded quiz |
| `GET /learning/quiz/{player_id}` | List a learner's generated quizzes |
| `GET /learning/integrations/status` | Honest iGOT/NSSTA configured-vs-fallback status |
| `GET /learning/admin/overview` | Aggregate-only organizational gap view |
| `GET /health`, `GET /`, `GET /docs` | Liveness, app info, Swagger UI |

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, React 19, Tailwind, Zustand, TanStack Query, React Flow, Recharts |
| Backend | FastAPI, SQLAlchemy, SQLite local demo + PostgreSQL 16/Alembic profile |
| AI | Google Gemini; optional `services/` upgrade adds `sentence-transformers` |
| Document ingestion | `pypdf`, `python-docx` (bounded — see `services/content_ingestion.py`) |

## For developers

<details>
<summary><strong>Full directory structure</strong> (click to expand)</summary>

```text
PRISM/
├── README.md
├── docs/
│   └── archive/                    Pre-rename planning documents (see docs/archive/README.md)
├── backend/
│   ├── .env.example
│   ├── main.py                    App entry point, CORS, router registration, DB init + gated demo seeding
│   ├── alembic.ini / migrations/ PostgreSQL migration history and startup revision contract
│   ├── requirements.txt / requirements-dev.txt
│   ├── db/
│   │   ├── database.py            Engine/session setup
│   │   └── seed.py                Demo DSA dungeon + seed_curricula_dungeons() for the other 3
│   ├── models/                    SQLAlchemy tables, including governance and identity bindings
│   │   ├── player.py, accuracy_history.py, dungeon.py, question.py, submission.py, guild.py, session.py
│   │   └── learning.py            LearnerProfile, CompetencyAssessment, LearningMaterial, GeneratedQuiz
│   ├── schemas/                   Learning, governance, identity and data-rights shapes
│   ├── security/                  OIDC, RBAC, audit, bootstrap, retention and data-rights primitives
│   ├── scripts/backup_restore.py  Local Docker/PostgreSQL backup/restore drill helper
│   ├── routes/
│   │   ├── game.py                All /game/* endpoints (now with cross-domain unlock fallback)
│   │   ├── ai_real.py             All /ai/* endpoints (calls Gemini directly)
│   │   └── learning.py            All /learning/* endpoints
│   ├── services/                  Pure logic — no DB, no HTTP (except learning_catalog's env read)
│   │   ├── game_logic.py, knowledge_graph.py, ai_client.py, heroes.py, monsters.py
│   │   ├── curricula.py           The 4-domain, 34-competency data-driven catalog
│   │   ├── learning_engine.py     Explainable gap/pathway calculation
│   │   ├── learning_catalog.py    Honest iGOT/NSSTA recommendation + status boundary
│   │   ├── content_ingestion.py   Bounded TXT/MD/PDF/DOCX extraction
│   │   └── quiz_generator.py      Grounded generation + deterministic fallback
│   └── tests/                     Legacy regression tests + lane-owned test_core_*.py suites
├── frontend/
│   ├── app/
│   │   ├── academy/                Wraps AcademyHub — profile, diagnostic, pathway, quiz upload
│   │   ├── admin/                  Aggregate organizational overview
│   │   ├── login/, register/, dungeon/, combat/[roomId]/, boss/[dungeonId]/, guild/, leaderboard/, stats/, dashboard/
│   ├── components/
│   │   ├── AcademyHub.jsx          The Academy's full UI
│   │   └── ...existing game/dashboard components + ui/ Pixel primitives
│   ├── lib/api/client.js           The ONLY file that calls fetch, now with a `learning` export
│   └── ...
└── services/                       Optional standalone AI engine (off by default)
```

</details>

- **Backend layout**: `routes/` = HTTP handlers only; `services/` = pure game/AI logic (no DB, no
  HTTP); `models/` = SQLAlchemy tables; `schemas/` = Pydantic request/response shapes.
- **Tests**:
  ```powershell
  cd backend
  & .\.venv\Scripts\Activate.ps1
  python -m pytest
  ```
  341 tests in the latest recorded full gate, with no server or API key required — every
  AI-dependent path under test either mocks the
  network call or exercises the deterministic fallback directly.
- **CORS**: the backend only allows credentialed requests from `FRONTEND_ORIGINS` — update it
  (comma-separated) if you deploy the frontend somewhere other than `localhost:3000`.
- **Provenance**: this project's dungeon-RPG engine and UI kernel are forked from an earlier,
  differently-branded adaptive DSA-learning game; the cross-domain skill-intelligence layer
  (`services/curricula.py`, `learning_engine.py`, `quiz_generator.py`, `routes/learning.py`,
  `components/AcademyHub.jsx`) originated in a separate AI-assisted design session and was
  integrated, completed, and verified here — see `docs/archive/README.md` for the full history.
