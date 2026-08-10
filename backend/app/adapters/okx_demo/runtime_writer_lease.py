from __future__ import annotations

from datetime import timedelta
import hashlib
import re
import threading
import time
from typing import Callable, Optional

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.models.execution_lineage import ExecutionScope
from app.models.order_writer import OkxOrderWriterLease


OKX_DEMO = "OKX_DEMO"
HEARTBEAT_SECONDS = 5.0
LEASE_SECONDS = 10
_HOLDER_TOKEN = re.compile(r"^[0-9a-f]{64}$")


class RuntimeWriterLeaseKeeper:
    """Keep the sole process writer fenced by the PostgreSQL lease.

    The heartbeat owns independent short transactions so a slow authenticated
    snapshot cannot make the database fence stale.  It deliberately shares the
    order writer's holder digest; order-level authorization remains enforced by
    the existing approval, grant, and dispatch checks.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        holder_token: str,
        on_failure: Callable[[], None],
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        lease_seconds: int = LEASE_SECONDS,
    ) -> None:
        if not _HOLDER_TOKEN.fullmatch(holder_token):
            raise OkxDemoWriteBlocked("runtime writer holder token is invalid")
        if heartbeat_seconds <= 0 or lease_seconds <= heartbeat_seconds:
            raise OkxDemoWriteBlocked("runtime writer lease timing is invalid")
        if lease_seconds > 30:
            raise OkxDemoWriteBlocked("runtime writer lease exceeds the risk fence")
        self._engine = engine
        self._holder_token_digest = hashlib.sha256(holder_token.encode()).hexdigest()
        self._on_failure = on_failure
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        self._generation: Optional[int] = None
        self._failure: Optional[Exception] = None
        self._failure_lock = threading.Lock()
        self._last_success_monotonic: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    @property
    def generation(self) -> Optional[int]:
        return self._generation

    def start(self) -> None:
        if self._closed:
            raise OkxDemoWriteBlocked("runtime writer lease keeper is closed")
        if self._thread is not None:
            raise OkxDemoWriteBlocked("runtime writer lease keeper already started")
        self._generation = self._maintain(expected_generation=None)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="okx-demo-writer-lease",
            daemon=True,
        )
        self._thread.start()

    def require_healthy(self) -> None:
        with self._failure_lock:
            failure = self._failure
            last_success = self._last_success_monotonic
        if (
            failure is not None
            or last_success is None
            or time.monotonic() - last_success >= self._lease_seconds
        ):
            raise OkxDemoWriteBlocked(
                "runtime writer database lease heartbeat failed"
            ) from None
        if self._generation is None:
            raise OkxDemoWriteBlocked("runtime writer database lease is unavailable")

    def publish(self, callback: Callable[[], None]) -> None:
        """Publish readiness without racing a heartbeat failure callback."""

        with self._failure_lock:
            fresh = (
                self._last_success_monotonic is not None
                and time.monotonic() - self._last_success_monotonic
                < self._lease_seconds
            )
            if self._failure is not None or self._generation is None or not fresh:
                raise OkxDemoWriteBlocked(
                    "runtime writer database lease is unavailable"
                )
            callback()

    def close(self) -> None:
        thread = self._thread
        self._stop.set()
        if thread is not None:
            thread.join(timeout=max(self._heartbeat_seconds * 2, 5.0))
            if thread.is_alive():
                raise OkxDemoWriteBlocked(
                    "runtime writer database lease heartbeat did not stop"
                )
        generation = self._generation
        self._thread = None
        if generation is not None:
            self._expire(generation)
        self._generation = None
        self._closed = True

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                generation = self._generation
                if generation is None:
                    raise OkxDemoWriteBlocked(
                        "runtime writer database lease generation is missing"
                    )
                self._maintain(expected_generation=generation)
            except Exception as exc:
                with self._failure_lock:
                    if self._failure is None:
                        self._failure = exc
                try:
                    self._on_failure()
                finally:
                    self._stop.set()
                return

    def _maintain(self, *, expected_generation: Optional[int]) -> int:
        with Session(self._engine) as db:
            try:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '2s'"))
                self._lock(db)
                self._require_target_contract(db)
                now = db.execute(text("SELECT clock_timestamp()")).scalar_one()
                lease = db.scalars(
                    select(OkxOrderWriterLease)
                    .where(OkxOrderWriterLease.execution_target_id == OKX_DEMO)
                    .with_for_update()
                ).first()
                expires_at = now + timedelta(seconds=self._lease_seconds)
                if lease is None:
                    if expected_generation is not None:
                        raise OkxDemoWriteBlocked(
                            "runtime writer database lease disappeared"
                        )
                    lease = OkxOrderWriterLease(
                        execution_target_id=OKX_DEMO,
                        holder_token_digest=self._holder_token_digest,
                        generation=1,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=expires_at,
                    )
                    db.add(lease)
                elif expected_generation is not None:
                    if (
                        lease.holder_token_digest != self._holder_token_digest
                        or lease.generation != expected_generation
                    ):
                        raise OkxDemoWriteBlocked(
                            "runtime writer database lease ownership changed"
                        )
                    lease.heartbeat_at = max(lease.heartbeat_at, now)
                    lease.expires_at = max(lease.expires_at, expires_at)
                elif lease.holder_token_digest == self._holder_token_digest:
                    lease.heartbeat_at = max(lease.heartbeat_at, now)
                    lease.expires_at = max(lease.expires_at, expires_at)
                elif lease.expires_at <= now:
                    lease.holder_token_digest = self._holder_token_digest
                    lease.generation += 1
                    lease.acquired_at = now
                    lease.heartbeat_at = now
                    lease.expires_at = expires_at
                else:
                    raise OkxDemoWriteBlocked(
                        "another OKX_DEMO writer holds the database lease"
                    )
                generation = int(lease.generation)
                db.commit()
                with self._failure_lock:
                    self._last_success_monotonic = time.monotonic()
                return generation
            except Exception:
                db.rollback()
                raise

    def _expire(self, generation: int) -> None:
        with Session(self._engine) as db:
            try:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '2s'"))
                self._lock(db)
                lease = db.scalars(
                    select(OkxOrderWriterLease)
                    .where(OkxOrderWriterLease.execution_target_id == OKX_DEMO)
                    .with_for_update()
                ).first()
                if (
                    lease is None
                    or lease.holder_token_digest != self._holder_token_digest
                    or lease.generation != generation
                ):
                    raise OkxDemoWriteBlocked(
                        "runtime writer database lease cannot be released"
                    )
                now = db.execute(text("SELECT clock_timestamp()")).scalar_one()
                release_at = max(now, lease.acquired_at + timedelta(microseconds=1))
                lease.heartbeat_at = release_at
                lease.expires_at = release_at
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _lock(db: Session) -> None:
        if db.get_bind().dialect.name != "postgresql":
            raise OkxDemoWriteBlocked(
                "runtime writer database lease requires PostgreSQL"
            )
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('OKX_DEMO-order-writer'))")
        )

    @staticmethod
    def _require_target_contract(db: Session) -> None:
        scope = db.get(ExecutionScope, OKX_DEMO)
        if (
            scope is None
            or scope.scope_kind != "EXCHANGE_TARGET"
            or scope.exchange_capable is not True
            or scope.executable is not False
            or scope.exchange_writes is not False
            or scope.order_submission_authorized is not False
        ):
            raise OkxDemoWriteBlocked(
                "OKX_DEMO target contract is missing or unsafe"
            )
