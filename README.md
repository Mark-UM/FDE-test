# Ecommerce Order Support Assistant

Small/cross-border ecommerce support agents answer questions such as “Where is my
order?” by manually collecting facts from order systems, logistics systems, and
warehouse notes. This product will bring those facts together into traceable
evidence and help an agent prepare a careful reply.

**Current implementation: Phase 0 foundation only.** The backend has `GET /health`;
the frontend displays a static project/status page. No business workflow is
implemented. PostgreSQL is provisioned for future work; the application does not
connect to it yet.

## Core V1 target

Inquiry → Authentication / Authorization → Order lookup → All parcels / Logistics
→ Warehouse notes → Evidence → CaseContext → Structured LLM analysis → Validation
→ Human review → Mock send → Audit / Metrics.

**Program owns facts. AI owns interpretation and wording.** Backend code controls
permissions, deterministic rules, freshness, and actions. Important claims must
trace to evidence. Facts, plans, and uncertainty stay distinct; cached/old data
cannot be presented as live. External systems enter through Provider interfaces.
Human review is required before sending.

This is not a general chatbot, SaaS/Agent platform, CRM, ERP, or ecommerce platform.
Core V1 excludes autonomous support, multi-tenancy, payment, inventory forecasting,
marketing, and execution of refunds, cancellations, address changes, or compensation.

## Repository

```text
backend/                    FastAPI, environment settings, tests, Dockerfile
frontend/                   React + TypeScript + Vite shell, Dockerfile
docs/
  plans/01_core_plan.md      Authoritative product roadmap
  plans/03_ecommerce_environment_plan.md  Future external Sandbox specification
  architecture.md           Responsibility boundaries and future pipeline
  scope.md                  Phase 0 and Core V1 scope
  external-system-contract.md  Conceptual external data contract
.github/workflows/ci.yml     Lint, tests, type/build, Compose configuration checks
AGENTS.md                   Persistent coding instructions
.env.example                Development configuration template
docker-compose.yml          Backend, frontend, PostgreSQL
```

The original root-level plans were moved unchanged into `docs/plans/` using the
canonical filenames above. Historical filenames referenced inside the plans refer
to those same documents.

## Local setup

Requires Python 3.12+ and Node.js 22.13+ within Node 22, or Node 24+
(Node 22 LTS is used in CI and Docker).
Docker Compose v2 is required only for the container setup.

Copy `.env.example` to `.env` at the repository root (`cp .env.example .env` on
POSIX, `Copy-Item .env.example .env` on PowerShell). Replace the example password in
both `POSTGRES_PASSWORD` and `DATABASE_URL`; URL-encode special characters in the
URL password. The example contains placeholders, not deployment credentials.

**Containers:** from the repository root:

```sh
docker compose config --quiet
docker compose up --build
```

Open frontend at `http://localhost:5173` and backend health at
`http://localhost:8000/health` (API docs: `http://localhost:8000/docs`). Ports can be
changed in `.env`. Services bind published ports to local loopback. PostgreSQL
uses a persistent named volume and Compose hostname `postgres`. Backend and frontend
source mounts support development reload; restart/rebuild after configuration or
dependency changes. `docker compose down` stops services and preserves database data.
These are development containers, not a production deployment.

**Host processes:** change the `DATABASE_URL` hostname in `.env` to `localhost`
(and use `POSTGRES_PORT` if changed). No database is needed for Phase 0 health.
Settings read the root `.env`; process environment variables take precedence.

```sh
cd backend
python -m venv .venv
# POSIX: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```sh
cd frontend
npm ci
npm run dev
```

Host CLI ports are explicit arguments; `BACKEND_PORT` and `FRONTEND_PORT` in `.env`
control Compose port publishing. The frontend currently makes no API calls.

## Checks

From `backend/`, with its virtual environment active:

```sh
ruff check .
ruff format --check .
pytest
```

From `frontend/`:

```sh
npm run lint
npm run typecheck
npm run build
```

From the repository root, validate without a personal `.env`:

```sh
docker compose --env-file .env.example config --quiet
```

CI runs all these checks on pushes and pull requests. `/health` returns exactly
`{"status":"ok","service":"ecommerce-order-support-backend"}`; it reports
process liveness, not database connectivity or external-system readiness.

## Next work and limitations

The next requested implementation step is **DemoCommerce Sandbox**, independently
built against [the conceptual contract](docs/external-system-contract.md). Its
internal database remains separate and accessible only through HTTP Providers.
The next product roadmap phase is **Phase 1: domain models, database migrations,
and seed data**. Neither begins as part of this bootstrap.

SQLAlchemy 2, Alembic, and a PostgreSQL driver will be introduced with database
work. HTTPX currently supports endpoint tests; runtime HTTP adapters come later.
React Router and TanStack Query are deferred until there are workflows to route
or fetch. Frontend dependencies are locked in `package-lock.json`; Python uses
bounded dependency ranges and does not yet have a full transitive lock.
