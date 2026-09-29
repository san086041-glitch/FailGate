from datetime import UTC, datetime

from sqlalchemy import update
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


async def mark_delivery(db: Database, delivery_id: str, **fields: object) -> None:
    """给投递记上 started_at / finished_at / case_id（排队延迟测量用）。

    没有对应行（测试里直接往队列放事件）时什么也不做。
    """
    async with db.session() as s:
        q = update(Delivery).where(Delivery.delivery_id == delivery_id).values(**fields)
        await s.execute(q)
        await s.commit()


def now() -> datetime:
    return datetime.now(UTC)
