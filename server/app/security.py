import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_settings().api_key
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
        )

