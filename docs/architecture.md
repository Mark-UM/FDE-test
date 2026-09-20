# Architecture freeze — Phase 0

The product authority is [the Core plan](plans/01_core_plan.md). The
[environment plan](plans/03_ecommerce_environment_plan.md) describes an external
test system only. This document describes the future target; only the health
endpoint, configuration, and frontend shell exist today.

## Canonical future pipeline

```text
Inquiry
→ Permission (authentication and order/inquiry authorization)
→ Providers (order, all parcels/logistics, warehouse notes)
→ Normalization
→ Evidence
→ CaseContext
→ LLM structured analysis
→ Validation
→ Human Review
→ MessageProvider
→ Audit / Metrics
```

## Responsibility boundaries

| Component | Owns | Boundary |
| --- | --- | --- |
| UI | Interaction, source/freshness display, draft editing and review | No external business-system calls or authorization decisions |
| Backend | Identity, authorization, orchestration, deterministic rules, freshness and actions | Check access before retrieving/disclosing facts or forming CaseContext |
| Providers | External-system boundaries and transport/error mapping | Return normalized facts with provenance; no implicit policy authority |
| Normalization | Source-field mapping, identifiers, timestamps, complete parcel association | Missing or unknown values remain explicit; do not invent facts |
| Evidence | Traceable facts with source system, record identity and timestamps | Separate facts, plans, uncertainty, conflicts and missing data |
| CaseContext | Minimum authorized evidence for one inquiry | The only business context supplied to AI |
| AI | Interpretation, summarization and wording | No database writes, permissions, freshness decisions or action execution |
| Validation | Schema, evidence references, freshness, risk and unsupported-claim checks | Prevent unsupported or unsafe outputs from flowing forward; fail to human handling |
| Human Review | Final approval of the actual reply text in Core V1 | Edits remain subject to deterministic validation; approval is not permission for high-risk actions |
| MessageProvider | Idempotent dispatch after backend-approved human review | Core V1 uses MockMessageProvider; no real channel send |
| Audit / Metrics | Sources, changes, approvals, send outcome, failures and processing time | Record failed paths as well as success; avoid sensitive payloads in ordinary logs |

**Program owns facts. AI owns interpretation and wording.** Prompts are not
security controls. Warehouse notes and customer messages remain untrusted text,
even when they contain instructions. High-risk actions are not executable in Core V1.

## External systems and data ownership

OrderProvider, LogisticsProvider, WarehouseProvider, LLMProvider and MessageProvider
are future interfaces, not implemented classes. Core services must depend on these
boundaries rather than vendor SDKs. Product PostgreSQL will hold product workflow
state; Sandbox SQLite will hold external simulated commerce state. Product code
must not query or share Sandbox database tables.

Core V1's baseline is Mock Providers. The separately planned Sandbox HTTP adapters
will use HTTPX and the [canonical contract](external-system-contract.md). The
environment plan's HTTP demo path is an integration follow-up, not a replacement
for the Core plan or permission to build the Sandbox in Phase 0.

Fetching an old record now does not make its business facts current. Application
code will determine freshness from original timestamps and explicit cache/failure
metadata; a cache read must retain its original `fetched_at`. Unknown source update
times remain unknown. Source failure, empty results and partial results are distinct;
a logistics timeout is not proof of a parcel exception. All parcels must be accounted
for before an order-wide statement is made. Retry/freshness thresholds are deferred.

## Runtime foundation today

- FastAPI + Pydantic v2 exposes a deterministic, dependency-independent `/health`.
- Pydantic Settings reads root `.env` and process variables (`APP_ENV`, `DATABASE_URL`).
  The URL is masked in representations and is not used to connect in Phase 0.
- React + TypeScript + Vite renders only a static title/status page.
- Development Compose provides backend, frontend and PostgreSQL. Backend startup
  waits for the PostgreSQL container health check, but `/health` never queries it.
- Pytest, Ruff, ESLint, TypeScript, Vite build and Compose config validation form CI.

No authentication, domain/database behavior, Provider implementation, Evidence
Engine, CaseContext resolver, LLM integration or support workbench is present.
SQLAlchemy 2, Alembic and the database driver are deferred to Phase 1; the
`postgresql+psycopg` DATABASE_URL convention reserves that future configuration.

