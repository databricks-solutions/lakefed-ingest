"""JDBC logins typed into the operations app, written into the dedicated secret scope.

A user gives a JDBC connection, the secret key (the connection's key, which holds one login per database when it has
{database} in it) and a user name and password. The app writes them as one JSON secret, {"user": ..., "password": ...},
into the scope the app bundle created for this (JDBC_LOGIN_SCOPE). The write goes through the Secrets API with the app's
own workspace client, so it shows under the app's service principal, never under the user's token. The app keeps one
jdbc_login_write row per write: who entered it, when, and whether it replaced an earlier login for the same scope and
key ('rotated').

The password is never stored in Lakebase, never logged and never sent back. It lives in a SecretStr, is redacted from
every error text, and the 422 for the login route keeps where and why a field failed but not the input it echoes.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

import jdbc_connections as jc
import lakebase

ROUTE = "/api/jdbc-logins"
SCOPE_ENV = "JDBC_LOGIN_SCOPE"
CONNECTION_MAX = 80
USER_NAME_MAX = 256
PASSWORD_MAX = 4096
REDACTED = "***"


def dedicated_scope() -> str:
    """The scope logins are written into: the app bundle's jdbc_login_scope, read from the environment."""
    return os.environ.get(SCOPE_ENV, "").strip()


class JdbcLoginRequest(BaseModel):
    """The body of POST /api/jdbc-logins. The secret key is the connection's key as the connection names it. A key with
    {database} in it takes the database the login is for, and the login is written under the key with that name in
    place of the token, the key a discovery reads for that database. The password is a SecretStr: its repr and str show
    asterisks, and only secret_value() reads it."""

    model_config = ConfigDict(extra="forbid")

    connection: str = Field(min_length=1, max_length=CONNECTION_MAX)
    secret_key: str = Field(min_length=1, max_length=128)
    database: Optional[str] = Field(default=None, max_length=128, validate_default=True)
    user_name: str = Field(min_length=1, max_length=USER_NAME_MAX)
    password: SecretStr

    @field_validator("connection", "user_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("this can't be blank")
        return value

    @field_validator("secret_key")
    @classmethod
    def _secret_key_shape(cls, value: str) -> str:
        if not jc.KEY_PATTERN.fullmatch(value.replace(jc.DATABASE_TOKEN, "db")):
            raise ValueError("the secret key may hold letters, digits and dashes, and {database} where each database's "
                             "name goes")
        return value

    @field_validator("database")
    @classmethod
    def _database_fills_the_key(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        template = info.data.get("secret_key")
        if template is None:
            return value
        name = (value or "").strip()
        if jc.DATABASE_TOKEN not in template:
            if name:
                raise ValueError("only a key with {database} in it takes a database")
            return None
        if not name:
            raise ValueError("this key has {database} in it: say which database the login is for")
        if not jc.SECRET_KEY_PATTERN.fullmatch(template.replace(jc.DATABASE_TOKEN, name)):
            raise ValueError("the database's name can't fill this key: a secret key holds letters, digits, dashes, "
                             "underscores and dots only, up to 128 characters, so a database whose name has a space or "
                             "any other character can't have a login of its own here")
        return name

    def resolved_secret_key(self) -> str:
        """The key the login is written under: the secret key with the database's name in place of {database}."""
        return self.secret_key.replace(jc.DATABASE_TOKEN, self.database or "")

    @field_validator("password")
    @classmethod
    def _password_size(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= PASSWORD_MAX:
            raise ValueError(f"the password must be 1 to {PASSWORD_MAX} characters")
        return value


def secret_value(user_name: str, password: SecretStr) -> str:
    """The JSON the engine reads from the secret: {"user": ..., "password": ...} on one line."""
    return json.dumps({"user": user_name, "password": password.get_secret_value()}, separators=(",", ":"))


def redact(text: str, *secrets: str) -> str:
    """The text with each secret replaced by asterisks, in its plain form and as it appears inside a JSON string."""
    out = str(text)
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, json.dumps(secret)[1:-1]}:
            out = out.replace(form, REDACTED)
    return out


def put_login(scope: str, secret_key: str, value: str) -> None:
    """Write the secret through the Secrets API as the app's own service principal (lakebase.workspace() authenticates
    with the app's credentials; the user's token is never used)."""
    lakebase.workspace().secrets.put_secret(scope=scope, key=secret_key, string_value=value)


def write_refusal(error: Exception, scope: str, secret_key: str, secrets: Iterable[str]) -> Tuple[int, str]:
    """The HTTP status and the message for a failed secret write, with the secrets it carried redacted."""
    if getattr(error, "status_code", None) == 403:
        return 403, (f"the app's service principal can't write to the secret scope {scope}: it needs WRITE on it, which "
                     "the app bundle grants. Nothing was saved.")
    first_line = redact(str(error).strip().split("\n")[0], *secrets)[:300]
    return 502, (f"Databricks refused the write to {scope}/{secret_key} ({first_line}). The login may not be saved; "
                 "try again.")


def safe_validation_errors(errors: List[dict]) -> List[dict]:
    """FastAPI's errors without their input (which echoes the field's value, a password included): where, what and why."""
    return [{"loc": list(error.get("loc", ())), "msg": str(error.get("msg", "invalid")),
             "type": str(error.get("type", "value_error"))} for error in errors]
