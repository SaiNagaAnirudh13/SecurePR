"""
Circuit Breaker.

Suspends/resumes *automatic approval only*. Analysis and commenting always
continue regardless of circuit breaker state, per the product requirement
that SecurePR keep working and keep humans informed even while paused.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.models import CircuitBreaker, CircuitBreakerEvent, CircuitScope


def is_tripped(db: Session, repo_id: str | None, author_login: str | None) -> tuple[bool, str | None]:
    """Returns (tripped, reason). Checks GLOBAL, then REPOSITORY, then USER scope."""
    now = datetime.utcnow()

    def _active(cb: CircuitBreaker) -> bool:
        if not cb.is_tripped:
            return False
        if cb.auto_resume_at and cb.auto_resume_at <= now:
            # auto-resume
            cb.is_tripped = False
            cb.resumed_at = now
            db.add(CircuitBreakerEvent(circuit_breaker_id=cb.id, action="AUTO_RESUME",
                                        actor="system", reason="Temporary suspension window elapsed"))
            db.commit()
            return False
        return True

    global_cb = db.query(CircuitBreaker).filter(CircuitBreaker.scope == CircuitScope.GLOBAL).first()
    if global_cb and _active(global_cb):
        return True, global_cb.reason or "Global circuit breaker is tripped."

    if repo_id:
        repo_cb = db.query(CircuitBreaker).filter(
            CircuitBreaker.scope == CircuitScope.REPOSITORY, CircuitBreaker.repository_id == repo_id
        ).first()
        if repo_cb and _active(repo_cb):
            return True, repo_cb.reason or "Repository circuit breaker is tripped."

    if author_login:
        user_cb = db.query(CircuitBreaker).filter(
            CircuitBreaker.scope == CircuitScope.USER, CircuitBreaker.user_login == author_login
        ).first()
        if user_cb and _active(user_cb):
            return True, user_cb.reason or f"Circuit breaker tripped for user '{author_login}'."

    return False, None


def trip(db: Session, scope: CircuitScope, actor: str, reason: str,
         repository_id: str | None = None, user_login: str | None = None,
         suspend_minutes: int | None = None) -> CircuitBreaker:
    query = db.query(CircuitBreaker).filter(CircuitBreaker.scope == scope)
    if scope == CircuitScope.REPOSITORY:
        query = query.filter(CircuitBreaker.repository_id == repository_id)
    elif scope == CircuitScope.USER:
        query = query.filter(CircuitBreaker.user_login == user_login)
    cb = query.first()
    if not cb:
        cb = CircuitBreaker(scope=scope, repository_id=repository_id, user_login=user_login)
        db.add(cb)

    cb.is_tripped = True
    cb.reason = reason
    cb.tripped_by = actor
    cb.tripped_at = datetime.utcnow()
    cb.resumed_at = None
    cb.auto_resume_at = datetime.utcnow() + timedelta(minutes=suspend_minutes) if suspend_minutes else None
    db.flush()
    db.add(CircuitBreakerEvent(circuit_breaker_id=cb.id, action="TRIP", actor=actor, reason=reason))
    db.commit()
    db.refresh(cb)
    return cb


def resume(db: Session, scope: CircuitScope, actor: str,
           repository_id: str | None = None, user_login: str | None = None) -> CircuitBreaker | None:
    query = db.query(CircuitBreaker).filter(CircuitBreaker.scope == scope)
    if scope == CircuitScope.REPOSITORY:
        query = query.filter(CircuitBreaker.repository_id == repository_id)
    elif scope == CircuitScope.USER:
        query = query.filter(CircuitBreaker.user_login == user_login)
    cb = query.first()
    if not cb:
        return None
    cb.is_tripped = False
    cb.resumed_at = datetime.utcnow()
    cb.auto_resume_at = None
    db.add(CircuitBreakerEvent(circuit_breaker_id=cb.id, action="RESUME", actor=actor, reason="Manually resumed"))
    db.commit()
    db.refresh(cb)
    return cb
