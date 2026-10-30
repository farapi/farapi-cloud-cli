"""The failures a command can end in, each with its own exit code (FR-30)."""

from __future__ import annotations

from typing import Any

OK = 0
API_ERROR = 1
USAGE = 2
NOT_SIGNED_IN = 3
UNREACHABLE = 4


class ApiError(Exception):
    """The server refused: the API's {code, params, fields} envelope."""

    exit_code = API_ERROR

    def __init__(
        self,
        status: int,
        code: str,
        params: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.params = params or {}
        self.fields = fields or {}


class NotSignedIn(Exception):
    """No sign-in for this API URL, or it was revoked or expired (FR-7)."""

    exit_code = NOT_SIGNED_IN
    code = "not_signed_in"


class Unreachable(Exception):
    """The server could not be reached at all."""

    exit_code = UNREACHABLE
    code = "unreachable"


class SignInFailed(Exception):
    """The browser or device sign-in ended without tokens: denied, expired,
    or refused by the authorization server."""

    exit_code = API_ERROR

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
