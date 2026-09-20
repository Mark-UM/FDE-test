# Scope freeze

## Current delivery: Phase 0 only

In scope: repository layout; FastAPI health endpoint and environment settings;
minimal React/TypeScript/Vite shell; tests/lint/build tooling; CI; development
Docker Compose with PostgreSQL; project instructions; architecture and external
data contracts documented without executable domain schemas.

Out of this phase: real order models, migrations, seed data, authentication,
authorization logic, Provider classes (including mocks), Evidence Engine,
CaseContext resolution, LLM calls, reply generation, support workbench,
Sandbox implementation, real logistics integration or any business action.

## Future Core V1 in scope

- Order-status support consultation for a small/cross-border ecommerce team.
- Authorized order, all-parcel/logistics and warehouse information retrieval.
- Evidence-based explanation with sources, timestamps and explicit uncertainty.
- Structured analysis and reply drafts with deterministic validation.
- Human review/edit/approval and idempotent mock sending.
- Auditability, basic metrics, and tests of failures, missing/conflicting/stale data.

## Explicitly out of Core V1

- Refund execution, cancellation execution, address modification, payment or
  compensation execution. Requests are routed to manual processes, not executed.
- CRM, ERP, inventory forecasting, marketing or a complete ecommerce platform.
- Multi-tenant SaaS, general Agent platforms, generic customer-service chatbots,
  or autonomous customer service.
- Real logistics/carrier and customer-channel integrations in the baseline release.

## Separate future environment

DemoCommerce Sandbox supplies simulated external facts, support messages and
controllable failures over HTTP. It contains no AI and is not this product's
database or a second ecommerce product. Its roadmap is separate from product
Phase 1. Only the conceptual contract is delivered here.

Any proposed feature must directly improve the defined order-support workflow
and be authorized for the current phase. Otherwise defer it.
