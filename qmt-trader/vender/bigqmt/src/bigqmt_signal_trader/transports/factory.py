"""Transport factory: pick a transport backend by name.

``build_transport(name, config, ...)`` returns a ready transport instance.
``name`` is the ``rpc.transport`` config value (default ``"redis"``). Unknown
names raise :class:`ValueError`. Optional dependencies (``zmq``, a mysql
driver) are imported lazily; a missing dependency surfaces as a clear
``ImportError`` only when that transport is actually selected.
"""

from .base import RpcTransport
from .redis_transport import RedisTransport


KNOWN_TRANSPORTS = ("redis", "zmq", "mysql", "shm")


def build_transport(
    name,
    config=None,
    account_id="",
    print_prefix="[bigqmt_rpc]",
):
    """Construct a transport by name.

    ``config`` is the ``rpc`` config dict. Each backend reads its own sub-keys
    (``config["zmq"]``, ``config["mysql"]``); Redis reads the legacy keys
    (``request_channel_template`` etc.) plus ``redis_client``/
    ``response_redis_client`` that the caller may inject.
    """
    config = dict(config or {})
    name = str(name or "redis").lower()

    if name in ("redis", "", "default"):
        return _build_redis(config, account_id, print_prefix)
    if name == "zmq":
        return _build_zmq(config, account_id, print_prefix)
    if name == "mysql":
        return _build_mysql(config, account_id, print_prefix)
    if name == "shm":
        return _build_shm(config, account_id, print_prefix)
    raise ValueError(
        "unknown rpc transport %r (known: %s)" % (name, ", ".join(KNOWN_TRANSPORTS))
    )


def _build_redis(config, account_id, print_prefix):
    redis_client = config.get("redis_client")
    if redis_client is None:
        from ..adapters.redis_common import build_redis_client

        redis_config = dict(config.get("redis") or {})
        redis_client = build_redis_client(redis_config)
    response_redis_client = config.get("response_redis_client")
    if response_redis_client is None:
        response_redis_client = redis_client
    return RedisTransport(
        redis_client,
        account_id=account_id,
        response_redis_client=response_redis_client,
        request_channel_template=config.get(
            "request_channel_template", "bigqmt:rpc:req:{account_id}"
        ),
        request_queue_template=config.get(
            "request_queue_template", "bigqmt:rpc:queue:{account_id}"
        ),
        response_channel_template=config.get(
            "response_channel_template", "bigqmt:rpc:resp:{account_id}:{request_id}"
        ),
        response_list_template=config.get(
            "response_list_template", "bigqmt:rpc:respq:{account_id}:{request_id}"
        ),
        response_key_template=config.get(
            "response_key_template", "bigqmt:rpc:resp:{account_id}:{request_id}"
        ),
        response_ttl_seconds=int(config.get("response_ttl_seconds", 60)),
        queue_poll_interval_seconds=float(config.get("queue_poll_interval_seconds", 0.02)),
        debug_log_limit=int(config.get("debug_log_limit", 0)),
        print_prefix=print_prefix,
    )


def _build_zmq(config, account_id, print_prefix):
    from .zmq_transport import ZmqTransport

    zmq_config = dict(config.get("zmq") or {})
    # Wire up service discovery: if the caller injected a redis_client (server
    # side) or provided redis connection settings, the ZMQ transport can
    # publish/look up the actual bound port when the default port is taken.
    discovery_client = zmq_config.get("discovery_redis_client")
    if discovery_client is None and config.get("redis_client") is not None:
        discovery_client = config.get("redis_client")
        zmq_config["discovery_redis_client"] = discovery_client
    if discovery_client is None and config.get("redis"):
        # Build a small client just for discovery from the redis config block.
        try:
            from ..adapters.redis_common import build_redis_client

            discovery_client = build_redis_client(dict(config.get("redis") or {}))
            zmq_config["discovery_redis_client"] = discovery_client
        except Exception:
            pass
    return ZmqTransport.from_config(
        zmq_config,
        account_id=account_id,
        print_prefix=print_prefix,
    )


def _build_mysql(config, account_id, print_prefix):
    from .mysql_transport import MysqlTransport

    return MysqlTransport.from_config(
        config.get("mysql") or {},
        account_id=account_id,
        print_prefix=print_prefix,
    )


def _build_shm(config, account_id, print_prefix):
    from .shm_transport import SharedMemoryTransport

    return SharedMemoryTransport(
        account_id=account_id,
        print_prefix=print_prefix,
        **dict(config.get("shm") or {})
    )
