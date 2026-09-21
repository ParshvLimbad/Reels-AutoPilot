"""Durable upload claims and confirmations. No network calls during migration."""
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from db import Session, Delivery, Reel


def blocked(account, code):
    with Session() as s:
        return s.query(Delivery).filter_by(account=account, code=code).first() is not None


def claim(account, code):
    with Session() as s:
        try:
            s.add(Delivery(account=account, code=code, status="uploading"))
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            return False


def uncertain(account, code, error):
    with Session() as s:
        row = s.query(Delivery).filter_by(account=account, code=code).one()
        if row.status in ("confirmed", "legacy_confirmed"):
            return
        row.status = "uncertain"
        row.error = str(error)[:200]
        s.commit()


def confirm(account, code, media):
    pk = str(getattr(media, "pk", "") or "")
    if not pk.isdigit():
        raise ValueError("Upload has no real destination media ID")
    with Session() as s:
        row = s.query(Delivery).filter_by(account=account, code=code).one()
        row.status = "confirmed"
        row.media_pk = pk
        row.media_code = str(getattr(media, "code", "") or "")
        row.confirmed_at = datetime.now()
        reel = s.query(Reel).filter_by(code=code).one()
        reel.is_posted = True
        reel.posted_at = row.confirmed_at
        history = [v for v in (reel.posted_by or "").split(",") if v]
        if account not in history:
            history.append(account)
        reel.posted_by = ",".join(history)
        s.commit()


def migrate_history():
    with Session() as s:
        for reel in s.query(Reel).all():
            owners = [v for v in (reel.posted_by or "").split(",") if v]
            if reel.is_posted and not owners and reel.assigned_to:
                owners = [reel.assigned_to]
            for owner in owners:
                if not s.query(Delivery).filter_by(account=owner, code=reel.code).first():
                    s.add(Delivery(account=owner, code=reel.code, status="legacy_confirmed",
                                   confirmed_at=reel.posted_at))
        # A process restart cannot prove the outcome of an in-flight upload.
        s.query(Delivery).filter_by(status="uploading").update({"status": "uncertain", "error": "Worker interrupted"})
        s.commit()
