import os
from db import Session, Reel, Delivery
import config
from helpers import print as log_print
import builtins

def console_print(message):
    builtins.print(message)
    log_print(message)

def purge_unposted():
    """Delete all unposted reels from DB and remove their files from disk."""
    session = Session()
    try:
        # Only purge truly fresh unposted reels, NOT swapped/recycled ones
        unposted = session.query(Reel).filter_by(is_posted=False).all()
        unposted = [r for r in unposted if not getattr(r, 'swap_phase', 0) or getattr(r, 'swap_phase', 0) == 0]
        held = {r.code for r in session.query(Delivery).all()}
        count = 0
        for reel in unposted:
            if reel.code in held or reel.posted_by:
                continue
            if reel.file_path and os.path.exists(reel.file_path):
                try:
                    os.remove(reel.file_path)
                except Exception as e:
                    console_print(f"[Purger] Failed to remove {reel.file_path}: {e}")
            session.delete(reel)
            count += 1
        session.commit()
        console_print(f"[Purger] Purged {count} unposted reels and files successfully.")
    except Exception as e:
        console_print(f"[Purger] Error purging: {e}")
        session.rollback()
    finally:
        session.close()

if __name__ == '__main__':
    purge_unposted()
