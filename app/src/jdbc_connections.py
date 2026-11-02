"""JDBC connections entered by hand: Discover's Create a connection, with the JDBC connection entered by hand option.

An admin creates the secret scope and the JSON secret ({"user", "password"}) beforehand, outside the app. The app keeps
only the names, in lakefed_ingest.jdbc_connection: the source type, host and port, which scope and key hold its login,
and whether the server's certificate is trusted. A discovery and the ingestion jobs made through a connection carry the
scope and the trust choice as job parameters, so the engine reads the source over Spark JDBC (ct_engine._read) and
never through a Unity Catalog connection.

The app never reads or writes a secret value here, and never calls the Secrets API. Nothing in this module can show
one: a connection's row holds names only.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

import queries as q

TABLE = "lakefed_ingest.jdbc_connection"
PORT_RANGE = (1, 65535)
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}")
HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_PATTERN = re.compile(rf"{HOST_LABEL}(?:\.{HOST_LABEL})*")
# The scope names schedules.PARAMETERS accepts for a job's secret_scope, so a connection's scope always passes there.
SCOPE_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
# Azure Key Vault's secret names (letters, digits and dashes), so a key works on a Key Vault-backed scope as well.
KEY_PATTERN = re.compile(r"[A-Za-z0-9-]{1,127}")
DATABASE_TOKEN = "{database}"
# What one database's name may be in a secret key: the characters a Databricks secret key holds. A name with anything else
# (a space, say) can't have a login stored under its key.
DATABASE_NAME_PATTERN = r"[A-Za-z0-9_.-]{1,128}"
# A Databricks secret key: letters, digits, dashes, underscores and dots, up to 128 characters. A key template with a
# database's name filled in, as discovery.secret_key_for fills it, must be one to hold that database's login.
SECRET_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
COLUMNS = ("name", "src_type", "src_host", "src_port", "secret_scope", "secret_key", "trust_server_certificate",
           "created_by", "created_at")


def trust_text(trusted: bool) -> str:
    """The job parameter and the stored value for a trust choice: the engine reads "true" or "false"."""
    return "true" if trusted else "false"


def is_trusted(stored) -> bool:
    """Whether a stored trust value (text, as the table keeps it) trusts the server's certificate."""
    return stored is True or str(stored or "").strip().lower() == "true"


def validate(body: dict) -> Tuple[dict, List[str]]:
    """The connection as it is stored, and the reasons it can't be (none when the list is empty). A blank port reads the
    source type's default port. The key may hold {database}, which a discovery fills with each database's name, so the
    key's rule is checked with that token replaced by a plain word."""
    errors: List[str] = []
    name = str(body.get("name") or "").strip()
    src_type = str(body.get("src_type") or "").strip().lower()
    host = str(body.get("src_host") or "").strip()
    scope = str(body.get("secret_scope") or "").strip()
    key = str(body.get("secret_key") or "").strip()
    port_given = body.get("src_port")
    if not NAME_PATTERN.fullmatch(name):
        errors.append("name the connection with letters, digits, spaces, dots, dashes or underscores, up to 64 "
                      "characters")
    if src_type not in q.SRC_TYPES:
        errors.append(f"source type must be one of {', '.join(q.SRC_TYPES)}")
    if not host:
        errors.append("host is required")
    elif len(host) > 253 or not HOST_PATTERN.fullmatch(host):
        errors.append("host must be a host name or an IPv4 address, with no scheme, path or port")
    port = q.DEFAULT_PORTS.get(src_type) if port_given in (None, "") else port_given
    if not isinstance(port, int) or isinstance(port, bool) or not PORT_RANGE[0] <= port <= PORT_RANGE[1]:
        errors.append(f"port must be a whole number from {PORT_RANGE[0]} to {PORT_RANGE[1]}")
    if not scope:
        errors.append("the secret scope is required: an admin creates it beforehand")
    elif not SCOPE_PATTERN.fullmatch(scope):
        errors.append("the secret scope name may hold letters, digits, dots, dashes, underscores and @, up to 128 "
                      "characters")
    if not key:
        errors.append("the secret key is required: the key in that scope that holds the login")
    elif not KEY_PATTERN.fullmatch(key.replace(DATABASE_TOKEN, "db")):
        errors.append("the secret key may hold letters, digits and dashes, and {database} where each database's "
                      "name goes, up to 127 characters")
    clean = {"name": name, "src_type": src_type, "src_host": host, "src_port": port, "secret_scope": scope,
             "secret_key": key, "trust_server_certificate": bool(body.get("trust_server_certificate"))}
    return clean, errors


def key_fits(template: str, key: str) -> bool:
    """Whether a secret key is one the connection's key template makes for some database: the {database} token stands for
    any name a key can hold, and the rest of the template must match exactly."""
    pieces = template.split(DATABASE_TOKEN)
    return bool(re.fullmatch(DATABASE_NAME_PATTERN.join(re.escape(piece) for piece in pieces), key))


def insert_sql() -> str:
    """Params: name, src_type, src_host, src_port, secret_scope, secret_key, trust_server_certificate ('true' or
    'false'), created_by. Returns the name when the row was made, nothing when the name is taken."""
    return (f"insert into {TABLE} (name, src_type, src_host, src_port, secret_scope, secret_key, "
            f"trust_server_certificate, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s) "
            f"on conflict (name) do nothing returning name")


def insert_params(clean: dict, created_by: Optional[str]) -> tuple:
    """The insert's params for a connection as validate returned it."""
    return (clean["name"], clean["src_type"], clean["src_host"], clean["src_port"], clean["secret_scope"],
            clean["secret_key"], trust_text(clean["trust_server_certificate"]), created_by or None)


def list_sql() -> str:
    """Every connection, by name. No params."""
    return f"select {', '.join(COLUMNS)} from {TABLE} order by lower(name)"


def get_sql() -> str:
    """One connection by its name. Param: name."""
    return f"select {', '.join(COLUMNS)} from {TABLE} where name = %s"


def public(row: dict) -> dict:
    """A stored connection as the API shows it: its names and where the source is, the trust choice in words, and who
    made it. It has no secret in it, because the table has none."""
    created = row.get("created_at")
    return {"name": row["name"], "src_type": row["src_type"], "src_host": row["src_host"],
            "src_port": row["src_port"], "secret_scope": row["secret_scope"], "secret_key": row["secret_key"],
            "trust": "trust" if is_trusted(row.get("trust_server_certificate")) else "verify",
            "created_by": row.get("created_by"), "created_at": _as_text(created)}


def _as_text(when) -> Optional[str]:
    """A stored time as ISO text. Postgres hands it back as a datetime or, through some reads, as text already."""
    if not when:
        return None
    return when if isinstance(when, str) else when.isoformat()


def job_parameters(row: dict) -> dict:
    """The ingestion job parameters a connection's jobs run with (a discovery's too): the scope the login is read from
    and the trust choice. A job made through a connection takes these over whatever its request said."""
    return {"secret_scope": row["secret_scope"],
            "trust_server_certificate": trust_text(is_trusted(row.get("trust_server_certificate")))}
