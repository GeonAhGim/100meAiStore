"""Internal single-ledger KRW budget contracts; no provider billing side effects."""
from datetime import datetime, timezone
import re

from .errors import ConflictError

PLATFORM_WARNING_MINOR = 24_000
PLATFORM_HARD_CAP_MINOR = 30_000
SQLITE_INTEGER_MAX = 2**63 - 1


def budget_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ConflictError('budget clock must be timezone aware')
    return value.astimezone(timezone.utc)


def validate_request_digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ConflictError('invalid budget request digest')


def validate_ledger_amount(value: int) -> None:
    if type(value) is not int or not 0 <= value < PLATFORM_HARD_CAP_MINOR:
        raise ConflictError('platform budget hard cap')


class BudgetRepositoryMixin:
    def get_budget_request(self, tenant_id, idempotency_key, request_digest):
        validate_request_digest(request_digest)
        with self.transaction():
            request = self._get_budget_request(tenant_id, idempotency_key)
            if request is not None:
                if request.request_digest != request_digest:
                    raise ConflictError('budget idempotency key reused')
                return request
            if any(e.idempotency_key == idempotency_key for e in self.budget_entries_for(tenant_id)):
                raise ConflictError('legacy budget request cannot be verified')
            return None

    def platform_monthly_budget_total(self, now):
        now = budget_time(now)
        with self.transaction():
            return sum(e.amount_minor for e in self._all_budget_entries()
                       if (budget_time(e.occurred_at).year, budget_time(e.occurred_at).month) == (now.year, now.month))

    def platform_budget_snapshot(self, now):
        now = budget_time(now)
        return {'reserved_minor': self.platform_monthly_budget_total(now), 'computed_at': now}

    def reserve_budget_entry(self, value):
        validate_ledger_amount(value.amount_minor)
        with self.transaction():
            prior = next((e for e in self.budget_entries_for(value.tenant_id)
                          if e.idempotency_key == value.idempotency_key), None)
            if prior is not None:
                return self.save_budget_entry(value)
            if self.platform_monthly_budget_total(value.occurred_at) + value.amount_minor >= PLATFORM_HARD_CAP_MINOR:
                raise ConflictError('platform budget hard cap')
            return self.save_budget_entry(value)
