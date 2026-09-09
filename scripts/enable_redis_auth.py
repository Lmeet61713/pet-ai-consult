"""Enable Redis authentication from the project's REDIS_PASSWORD setting."""
from __future__ import annotations

import redis

from app.core.config import Settings


def main() -> None:
    settings = Settings()
    if not settings.redis_password:
        raise RuntimeError("REDIS_PASSWORD is empty")

    unauthenticated = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    secured = redis.Redis.from_url(
        settings.redis_url,
        password=settings.redis_password,
        decode_responses=True,
    )

    try:
        key_count = unauthenticated.dbsize()
        unauthenticated.save()
        unauthenticated.config_set("requirepass", settings.redis_password)
    except redis.AuthenticationError:
        key_count = secured.dbsize()

    if not secured.ping() or secured.dbsize() != key_count:
        raise RuntimeError("Redis authentication verification failed")
    print(f"redis_auth_enabled=yes keys={key_count}")


if __name__ == "__main__":
    main()
