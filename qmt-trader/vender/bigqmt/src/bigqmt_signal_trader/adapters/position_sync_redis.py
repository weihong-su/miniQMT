"""Publish Big QMT position snapshots to Redis."""

import datetime as _dt
import json


class RedisPositionSyncSink:
    def __init__(
        self,
        redis_client,
        key_template="bigqmt:positions:{account_id}",
        event_stream_template="bigqmt:position_events:{account_id}",
        ttl_seconds=120,
        publish_events=True,
    ):
        self.redis = redis_client
        self.key_template = key_template
        self.event_stream_template = event_stream_template
        self.ttl_seconds = int(ttl_seconds)
        self.publish_events = bool(publish_events)

    @staticmethod
    def _time_text(value):
        if isinstance(value, _dt.datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def _snapshot_to_dict(self, snapshot):
        return {
            "account_id": snapshot.account_id,
            "reason": snapshot.reason,
            "updated_at": self._time_text(snapshot.updated_at),
            "asset": {
                "cash": snapshot.asset.cash,
                "total_asset": snapshot.asset.total_asset,
            },
            "positions": {
                code: {
                    "stock_code": position.stock_code,
                    "volume": position.volume,
                    "available": position.available,
                    "cost": position.cost,
                    "stock_name": position.stock_name,
                }
                for code, position in snapshot.positions.items()
            },
        }

    def publish(self, snapshot):
        payload = json.dumps(self._snapshot_to_dict(snapshot), ensure_ascii=False)
        key = self.key_template.format(account_id=snapshot.account_id)
        if self.ttl_seconds > 0:
            self.redis.setex(key, self.ttl_seconds, payload)
        else:
            self.redis.set(key, payload)
        if self.publish_events:
            stream_key = self.event_stream_template.format(account_id=snapshot.account_id)
            self.redis.xadd(stream_key, {"payload": payload})
