"""Trusted operator entry point; never exposed as a Product HTTP route."""

import argparse
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.clock import SystemClock
from app.core.config import Settings
from app.db.connection import product_engine
from app.services.context_models import FreshnessPolicy
from app.services.resolution import ResolutionService


def main():
    parser = argparse.ArgumentParser(description="Fail one interrupted RUNNING resolution")
    parser.add_argument("--inquiry-id", type=UUID, required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--expected-lock-version", type=int, required=True)
    parser.add_argument("--confirm-interrupted", action="store_true", required=True)
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("DATABASE_URL is required")
    engine = product_engine(settings.database_url.get_secret_value())
    try:
        ResolutionService(
            lambda: Session(engine), SystemClock(), "operator-recovery", FreshnessPolicy()
        ).recover(args.inquiry_id, args.run_id, args.expected_lock_version)
    except Exception:
        parser.exit(1, "Recovery rejected or unavailable; inspect safe database state.\n")
    finally:
        engine.dispose()
    print("Resolution marked FAILED/INTERRUPTED; no sources fetched.")


if __name__ == "__main__":
    main()
