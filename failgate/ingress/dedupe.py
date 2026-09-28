from sqlalchemy.exc import IntegrityError

from failgate.db import Database, Delivery


async def first_seen(db: Database, platform: str, delivery_id: str, event: str) -> bool:
    """记录一次投递；同一 delivery id 第二次出现时返回 False。"""
    async with db.session() as s:
        s.add(Delivery(delivery_id=delivery_id, platform=platform, event=event))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return False
    return True
