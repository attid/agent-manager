"""Durable storage for analysis reports and approval-gated recommendations.

This module owns persistence only. Building the analyzer evidence package,
running the analyzer and applying an approved recommendation are domain
concerns of :mod:`agent_manager.analysis`.

Protocol
--------

* An :class:`~agent_manager.storage.models.AnalysisReport` is written exactly
  once a structured analyzer document has been parsed. A failed analyzer run
  fails the owning operation and writes no report, so an absent report is
  never mistaken for a clean one.
* A :class:`~agent_manager.storage.models.Recommendation` is created
  ``proposed`` and never mutates its target. Approval moves it to
  ``approved``; applying it moves it to ``applied``. A stale target moves it
  to ``conflict`` without overwriting anything. Invalid transitions raise
  :class:`RecommendationStateError`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select, update

from .database import Database
from .models import (
    RECOMMENDATION_STATUS_APPLIED,
    RECOMMENDATION_STATUS_APPROVED,
    RECOMMENDATION_STATUS_CONFLICT,
    RECOMMENDATION_STATUS_FAILED,
    RECOMMENDATION_STATUS_PROPOSED,
    RECOMMENDATION_STATUSES,
    AnalysisReport,
    Recommendation,
    utcnow,
)


class AnalysisError(Exception):
    """Base class for analysis storage failures."""


class AnalysisReportNotFoundError(AnalysisError):
    """Raised when an analysis report id does not exist."""

    def __init__(self, report_id: str) -> None:
        super().__init__(f"analysis report not found: {report_id}")
        self.report_id = report_id


class RecommendationNotFoundError(AnalysisError):
    """Raised when a recommendation id does not exist."""

    def __init__(self, recommendation_id: str) -> None:
        super().__init__(f"recommendation not found: {recommendation_id}")
        self.recommendation_id = recommendation_id


class RecommendationStateError(AnalysisError):
    """Raised when a transition is attempted from an invalid status."""

    def __init__(self, recommendation_id: str, status: str, action: str) -> None:
        super().__init__(
            f"cannot {action} recommendation {recommendation_id} "
            f"in status {status!r}"
        )
        self.recommendation_id = recommendation_id
        self.status = status
        self.action = action


class AnalysisRepository:
    """Create and read analysis reports and their recommendations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- reports ---------------------------------------------------------

    def create_report(
        self,
        *,
        task_id: str,
        agent_id: str,
        profile_revision: int | None,
        summary: str | None,
        report: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> AnalysisReport:
        """Persist one structured analysis report and return the row."""
        row = AnalysisReport(
            id=uuid.uuid4().hex,
            task_id=task_id,
            agent_id=agent_id,
            profile_revision=profile_revision,
            summary=summary,
            report=dict(report),
            evidence=dict(evidence),
        )
        with self._database.session() as session:
            session.add(row)
            session.flush()
        return row

    def get_report(self, report_id: str) -> AnalysisReport | None:
        """Return the report with ``report_id`` or ``None``."""
        with self._database.session() as session:
            return session.scalars(
                select(AnalysisReport).where(AnalysisReport.id == report_id)
            ).one_or_none()

    def list_reports(
        self,
        *,
        task_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AnalysisReport]:
        """Return reports, newest first, optionally filtered by task."""
        statement = select(AnalysisReport).order_by(
            AnalysisReport.sequence.desc()
        )
        if task_id is not None:
            statement = statement.where(AnalysisReport.task_id == task_id)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))

    # -- recommendations -------------------------------------------------

    def create_recommendation(
        self,
        *,
        report_id: str,
        task_id: str | None,
        target_kind: str,
        target_id: str,
        title: str,
        rationale: str,
        evidence: Sequence[Any],
        proposed_content: Any,
        diff: str | None = None,
        target_revision: int | None = None,
        target_content_hash: str | None = None,
    ) -> Recommendation:
        """Persist one ``proposed`` recommendation; never mutates a target."""
        if target_kind not in {"profile", "project", "component"}:
            raise ValueError(f"unsupported recommendation target_kind: {target_kind!r}")
        row = Recommendation(
            id=uuid.uuid4().hex,
            report_id=report_id,
            task_id=task_id,
            target_kind=target_kind,
            target_id=target_id,
            title=title,
            rationale=rationale,
            evidence=[dict(item) if isinstance(item, Mapping) else item
                      for item in evidence],
            proposed_content=proposed_content,
            diff=diff,
            target_revision=target_revision,
            target_content_hash=target_content_hash,
            status=RECOMMENDATION_STATUS_PROPOSED,
        )
        with self._database.session() as session:
            session.add(row)
            session.flush()
        return row

    def get_recommendation(
        self, recommendation_id: str
    ) -> Recommendation | None:
        """Return the recommendation with ``recommendation_id`` or ``None``."""
        with self._database.session() as session:
            return session.scalars(
                select(Recommendation).where(
                    Recommendation.id == recommendation_id
                )
            ).one_or_none()

    def list_recommendations(
        self,
        *,
        task_id: str | None = None,
        report_id: str | None = None,
        status: str | None = None,
        target_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Recommendation]:
        """Return recommendations ordered by intake sequence, oldest first."""
        statement = select(Recommendation).order_by(Recommendation.sequence)
        if task_id is not None:
            statement = statement.where(Recommendation.task_id == task_id)
        if report_id is not None:
            statement = statement.where(Recommendation.report_id == report_id)
        if status is not None:
            statement = statement.where(Recommendation.status == status)
        if target_kind is not None:
            statement = statement.where(
                Recommendation.target_kind == target_kind
            )
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._database.session() as session:
            return list(session.scalars(statement))

    def approve_recommendation(
        self, recommendation_id: str
    ) -> Recommendation:
        """Move a ``proposed`` recommendation to ``approved``.

        Only ``proposed -> approved`` is accepted. Repeating the approval of an
        already-approved recommendation is idempotent. Any other status raises
        :class:`RecommendationStateError`.
        """
        return self._transition(
            recommendation_id,
            action="approve",
            from_statuses={RECOMMENDATION_STATUS_PROPOSED},
            to_status=RECOMMENDATION_STATUS_APPROVED,
            idempotent_statuses={RECOMMENDATION_STATUS_APPROVED},
        )

    def mark_applied(
        self,
        recommendation_id: str,
        *,
        applied_revision: int | None,
        applied_content_hash: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> Recommendation:
        """Record a successful apply of an ``approved`` recommendation."""
        return self._transition(
            recommendation_id,
            action="apply",
            from_statuses={RECOMMENDATION_STATUS_APPROVED},
            to_status=RECOMMENDATION_STATUS_APPLIED,
            idempotent_statuses={RECOMMENDATION_STATUS_APPLIED},
            extra_values={
                "applied_revision": applied_revision,
                "applied_content_hash": applied_content_hash,
                "apply_detail": dict(detail) if detail is not None else None,
            },
        )

    def mark_conflict(
        self,
        recommendation_id: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> Recommendation:
        """Record a stale-target conflict without overwriting the target."""
        return self._transition(
            recommendation_id,
            action="apply",
            from_statuses={RECOMMENDATION_STATUS_APPROVED},
            to_status=RECOMMENDATION_STATUS_CONFLICT,
            idempotent_statuses={RECOMMENDATION_STATUS_CONFLICT},
            extra_values={
                "apply_detail": dict(detail) if detail is not None else None,
            },
        )

    def mark_failed(
        self,
        recommendation_id: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> Recommendation:
        """Record an apply failure that is neither a conflict nor a success."""
        return self._transition(
            recommendation_id,
            action="apply",
            from_statuses={RECOMMENDATION_STATUS_APPROVED},
            to_status=RECOMMENDATION_STATUS_FAILED,
            idempotent_statuses={RECOMMENDATION_STATUS_FAILED},
            extra_values={
                "apply_detail": dict(detail) if detail is not None else None,
            },
        )

    # -- internals -------------------------------------------------------

    def _transition(
        self,
        recommendation_id: str,
        *,
        action: str,
        from_statuses: set[str],
        to_status: str,
        idempotent_statuses: set[str],
        extra_values: Mapping[str, Any] | None = None,
    ) -> Recommendation:
        assert to_status in RECOMMENDATION_STATUSES
        with self._database.session() as session:
            row = session.scalars(
                select(Recommendation).where(
                    Recommendation.id == recommendation_id
                )
            ).one_or_none()
            if row is None:
                raise RecommendationNotFoundError(recommendation_id)
            if row.status in idempotent_statuses:
                return row
            if row.status not in from_statuses:
                raise RecommendationStateError(
                    recommendation_id, row.status, action
                )
            values: dict[str, Any] = {
                "status": to_status,
                "updated_at": utcnow(),
            }
            if extra_values:
                values.update(extra_values)
            result = session.execute(
                update(Recommendation)
                .where(
                    Recommendation.id == recommendation_id,
                    Recommendation.status == row.status,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                session.refresh(row)
                raise RecommendationStateError(
                    recommendation_id, row.status, action
                )
            session.refresh(row)
            return row


__all__ = [
    "AnalysisError",
    "AnalysisReportNotFoundError",
    "AnalysisRepository",
    "RecommendationNotFoundError",
    "RecommendationStateError",
]
