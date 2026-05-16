import unittest

from core.notification_worker import NOTIFICATIONS_PENDING_KEY, _claim_due_notification_ids


class FakeRedis:
    def __init__(self, entries):
        self.entries = dict(entries)

    async def zpopmin(self, key, count):
        assert key == NOTIFICATIONS_PENDING_KEY
        if not self.entries:
            return []
        member, score = sorted(self.entries.items(), key=lambda item: item[1])[0]
        del self.entries[member]
        return [(member, score)]

    async def zadd(self, key, mapping):
        assert key == NOTIFICATIONS_PENDING_KEY
        self.entries.update(mapping)


class NotificationWorkerClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_due_ids_removes_due_entries_atomically(self):
        redis = FakeRedis({"due-1": 10, "due-2": 20})

        claimed = await _claim_due_notification_ids(redis, now_ts=30, batch_size=10)

        self.assertEqual(claimed, ["due-1", "due-2"])
        self.assertEqual(redis.entries, {})

    async def test_claim_stops_and_restores_first_future_entry(self):
        redis = FakeRedis({"due": 10, "future": 40})

        claimed = await _claim_due_notification_ids(redis, now_ts=30, batch_size=10)

        self.assertEqual(claimed, ["due"])
        self.assertEqual(redis.entries, {"future": 40})

    async def test_claim_respects_batch_limit(self):
        redis = FakeRedis({"due-1": 10, "due-2": 20, "due-3": 25})

        claimed = await _claim_due_notification_ids(redis, now_ts=30, batch_size=2)

        self.assertEqual(claimed, ["due-1", "due-2"])
        self.assertEqual(redis.entries, {"due-3": 25})


if __name__ == "__main__":
    unittest.main()
