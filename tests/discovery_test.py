"""Table discovery: recommendations per table, assembly of the catalog queries, helpers."""
import re

import pytest

import discovery as d


def _cols(*specs):
    """specs: (name, base_type[, user_type, is_identity])"""
    out = []
    for i, s in enumerate(specs, 1):
        name, base = s[0], s[1]
        out.append({"pos": i, "col": name, "base_type": base, "user_type": s[2] if len(s) > 2 else base,
                    "is_identity": s[3] if len(s) > 3 else 0})
    return out


T = {"sch": "dbo", "tbl": "t", "row_count": 10, "size_mb": 1.0, "ct_enabled": 1}


def test_ct_table_with_pk_recommends_ct():
    r = d.recommend(T, _cols(("id", "int", "int", 1), ("v", "nvarchar")), ["id"])
    assert (r["load_type"], r["cursor_column"], r["primary_key"]) == ("ct", None, "id")
    assert r["select_list"] == "id, v" and r["partition_candidate"] == "id" and r["identity_column"] == "id"


def test_no_ct_with_rowversion_recommends_cursor():
    r = d.recommend(dict(T, ct_enabled=0), _cols(("id", "bigint"), ("rv", "timestamp")), ["id"])
    assert (r["load_type"], r["cursor_column"]) == ("cursor", "rv") and "deletes not captured" in r["notes"]


def test_no_pk_or_no_rowversion_falls_back_to_full():
    assert d.recommend(T, _cols(("a", "int"), ("rv", "timestamp")), [])["load_type"] == "full"     # CT but no PK
    r = d.recommend(dict(T, ct_enabled=0), _cols(("id", "int"), ("upd", "datetime2")), ["id"])
    assert r["load_type"] == "full" and r["cursor_column"] is None                                 # never auto-picks updated_at


def test_unsupported_columns_are_excluded_and_reported():
    r = d.recommend(T, _cols(("id", "int"), ("geo", "varbinary", "geography"), ("x", "sql_variant"),
                             ("bad,name", "int"), ("v", "int")), ["id"])
    assert r["select_list"] == "id, v" and r["excluded_columns"] == "geo, x, bad,name"
    assert "excluded unsupported columns" in r["notes"]


def test_pk_on_unsupported_type_is_dropped():
    r = d.recommend(T, _cols(("h", "varbinary", "hierarchyid"), ("v", "int")), ["h"])
    assert r["primary_key"] is None and r["load_type"] == "full"


def test_partition_candidate_rules():
    assert d.recommend(T, _cols(("k", "nvarchar"), ("n", "int", "int", 1)), ["k"])["partition_candidate"] == "n"   # identity fallback
    assert d.recommend(T, _cols(("k", "nvarchar")), ["k"])["partition_candidate"] is None
    assert d.recommend(T, _cols(("d", "date"), ("k", "int")), ["d", "k"])["partition_candidate"] == "d"


def test_assemble_joins_queries_and_orders_composite_pk():
    tables = [{"oid": 2, "sch": "dbo", "tbl": "b", "row_count": 5, "size_mb": 2.5, "ct_enabled": 0},
              {"oid": 1, "sch": "dbo", "tbl": "A", "row_count": None, "size_mb": None, "ct_enabled": 1}]
    columns = [dict(c, oid=1) for c in _cols(("x", "int"), ("y", "int"))] + [dict(c, oid=2) for c in _cols(("z", "int"))]
    pks = [{"oid": 1, "ord": 2, "col": "y"}, {"oid": 1, "ord": 1, "col": "x"}]
    out = d.assemble(tables, columns, pks)
    assert [r["src_table"] for r in out] == ["A", "b"]
    assert out[0]["primary_key"] == "x, y" and out[0]["load_type"] == "ct" and out[0]["row_count"] == 0
    assert out[1]["primary_key"] is None and out[1]["load_type"] == "full" and out[1]["size_mb"] == 2.5


def test_catalog_queries_read_metadata_only():
    for sql in (d.TABLES_SQL, d.COLUMNS_SQL, d.PK_SQL):
        assert "is_ms_shipped = 0" in sql and "sys." in sql
    assert "sys.allocation_units" in d.TABLES_SQL and "sys.change_tracking_tables" in d.TABLES_SQL


def test_helpers():
    assert d.secret_key_for({"secret_key_template": "creds-{database}"}, "db1") == "creds-db1"
    assert d.secret_key_for({"secret_key": "k"}, "db1") == "k"


def test_other_sources_are_placeholders(monkeypatch):
    with pytest.raises(ValueError, match="not implemented yet for src_type 'oracle'"):
        d.discover_database({"src_type": "oracle"})


def test_discover_database_retries_connection_failures_only(monkeypatch):
    import ct_engine
    calls, sleeps = [], []

    class _R:
        def __init__(self, rows): self.rows = rows
        def collect(self): return [type("Row", (), {"asDict": lambda s, r=r: r})() for r in self.rows]

    def read(spark, cfg, sql, **kw):
        calls.append(sql)
        if len(calls) == 1:
            raise RuntimeError("[FAILED_JDBC.CONNECTION] Failed to connect to the database")
        if sql == d.TABLES_SQL:
            return _R([{"oid": 1, "sch": "dbo", "tbl": "t", "row_count": 1, "size_mb": 1, "ct_enabled": 1}])
        if sql == d.COLUMNS_SQL:
            return _R([{"oid": 1, "pos": 1, "col": "id", "base_type": "int", "user_type": "int", "is_identity": 0}])
        return _R([{"oid": 1, "ord": 1, "col": "id"}])
    monkeypatch.setattr(ct_engine, "_spark", lambda: None)
    monkeypatch.setattr(ct_engine, "_read", read)
    out = d.discover_database({"src_type": "sqlserver"}, sleep=sleeps.append, rand=lambda: 1.0)
    assert [r["load_type"] for r in out] == ["ct"] and sleeps == [d.CONNECT_RETRY_BASE_S] and len(calls) == 4

    def bad(spark, cfg, sql, **kw):        # Spark wraps a refused login as a connection failure too
        raise RuntimeError("[FAILED_JDBC.CONNECTION] Failed to connect: Login failed for user 'x'")
    monkeypatch.setattr(ct_engine, "_read", bad)
    with pytest.raises(RuntimeError, match="Login failed"):
        d.discover_database({"src_type": "sqlserver"}, sleep=sleeps.append)
    assert len(sleeps) == 1                                   # permanent errors are not retried


def test_discover_database_waits_for_a_paused_database_with_backoff(monkeypatch):
    import ct_engine
    sleeps = []

    def paused(spark, cfg, sql, **kw):
        raise RuntimeError("[FAILED_JDBC.CONNECTION] Failed JDBC | caused by SQLServerException[code=40613,state=S0001]: "
                           "Database 'db' on server 's' is not currently available.")
    monkeypatch.setattr(ct_engine, "_spark", lambda: None)
    monkeypatch.setattr(ct_engine, "_read", paused)
    with pytest.raises(RuntimeError, match="40613"):
        d.discover_database({"src_type": "sqlserver"}, sleep=sleeps.append, rand=lambda: 1.0)
    assert sleeps == [30.0, 60.0, 120.0]                      # 3 retries, doubling, capped at 120 s


def test_nullable_rowversion_is_not_a_cursor_candidate():
    cols = _cols(("id", "int"), ("rv", "timestamp"))
    cols[1]["is_nullable"] = 1
    rec = d.recommend({"ct_enabled": 0}, cols, ["id"])
    assert rec["load_type"] == "full" and rec["cursor_column"] is None
    assert "is_nullable" in d.COLUMNS_SQL


def test_a_discovery_through_a_uc_connection_reads_with_no_secret():
    cfg = d.cfg_for({"src_connection": "sql_a"}, "eng_001")
    assert cfg == {"id": 0, "src_type": "sqlserver", "src_database": "eng_001", "src_connection": "sql_a",
                   "use_remote_query": True}
    jdbc = d.cfg_for({"src_host": "h1", "src_port": 1433, "secret_key_template": "creds-{database}"}, "eng_001")
    assert (jdbc["src_host"], jdbc["secret_key"]) == ("h1", "creds-eng_001") and "use_remote_query" not in jdbc


def test_listing_keeps_how_a_discovery_listed_but_never_the_secret():
    spec = {"src_host": "h1", "secret_key": "s3cret", "secret_key_template": "creds-{database}", "pattern": "lfc_%",
            "azure_credential": "", "azure_resource_id": "/subscriptions/x", "databases": "*"}
    assert d.listing_of(spec) == {"pattern": "lfc_%", "secret_key_template": "creds-{database}",
                                  "azure_resource_id": "/subscriptions/x"}
    assert d.listing_of({"src_connection": "sql_a", "databases": ["a"]}) == {}
    assert d.listing_of({"src_host": "h1", "secret_key": "creds-a", "jdbc_connection": "orders_jdbc"}) == {
        "jdbc_connection": "orders_jdbc"}


@pytest.mark.parametrize("spec,error", [
    ({"databases": ["a"]}, "src_connection (a UC connection) or src_host"),
    ({"src_host": "h1", "databases": ["a"]}, "secret_key"),
    ({"src_connection": "sql_a"}, "databases"),
])
def test_a_spec_says_what_it_lacks(spec, error):
    assert any(error in e for e in d.spec_errors(spec))


def test_listing_only_needs_a_connection_and_no_databases():
    assert d.spec_errors({"src_connection": "sql_a", "kind": "list"}) == []
    assert d.spec_errors({"src_connection": "sql_a", "databases": "*"}) == []


def test_a_servers_databases_are_read_through_master():
    seen = []
    names = d.list_databases({"src_connection": "sql_a"}, query=lambda cfg, sql: seen.append((cfg, sql)) or
                             [{"name": "eng_001"}, {"name": "eng_002"}])
    assert names == ["eng_001", "eng_002"]
    cfg, sql = seen[0]
    assert cfg["src_database"] == "master" and cfg["use_remote_query"] and sql == d.databases_sql("")
    assert "database_id > 4" in sql and "ONLINE" in sql
    with pytest.raises(ValueError):
        d.list_databases({"src_connection": "ora", "src_type": "oracle"}, query=lambda cfg, sql: [])


def test_databases_are_listed_by_name_not_by_access_through_master():
    # A contained user in each database has no access through master, so HAS_DBACCESS would list none of them.
    assert "HAS_DBACCESS" not in d.databases_sql("")
    seen = []
    d.list_databases({"src_connection": "sql_a", "pattern": "lfc_eng_%"}, query=lambda cfg, sql: seen.append(sql) or [])
    assert seen == ["SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' "
                    "AND name LIKE N'lfc_eng_%'"]


@pytest.mark.parametrize("pattern", ["x' OR 1=1 --", "a b", "a;drop", "x" * 129])
def test_a_filter_that_isnt_a_plain_name_pattern_is_refused(pattern):
    assert d.pattern_errors(pattern) and d.spec_errors({"src_connection": "sql_a", "kind": "list", "pattern": pattern})
    with pytest.raises(ValueError):
        d.databases_sql(pattern)


@pytest.mark.parametrize("pattern", ["", "lfc_eng_%", "eng_0[0-4]%", "sales-2024.%"])
def test_name_patterns_as_like_reads_them_are_taken(pattern):
    assert d.pattern_errors(pattern) == []


# The two lab connections, as found on 29 Oct: one whose login is a user contained in each database (it opens
# every lab database but not master), and one whose login is a server login with ##MS_DefinitionReader## (master
# lists all 201 databases, though HAS_DBACCESS there is 0 for every one of them).
LAB = ([f"lfc_eng_{i:03d}" for i in range(1, 101)] + ["lfc_test"] + [f"lfc_test_{i:03d}" for i in range(1, 51)]
       + [f"lfc_heavy_{i:03d}" for i in range(1, 51)])
CANT_OPEN_MASTER = ("[FAILED_JDBC.CONNECTION] Failed JDBC on the operation: Failed to connect to the database. "
                    "SQLSTATE: HV000")


def _server_login(cfg, sql):
    """master as the server login sees it: every database by name, whatever HAS_DBACCESS would say."""
    assert cfg["src_database"] == "master" and "HAS_DBACCESS" not in sql
    like = re.search(r"name LIKE N'([^']*)'", sql)
    pattern = re.compile("^" + "".join(".*" if c == "%" else "." if c == "_" else re.escape(c)
                                       for c in (like.group(1) if like else "%")) + "$")
    return [{"name": n} for n in LAB if pattern.match(n)]


def _contained_user(cfg, sql):
    if cfg["src_database"] == "master":
        raise RuntimeError(CANT_OPEN_MASTER)
    return []


def test_a_contained_user_cant_list_and_its_typed_databases_are_read_by_name_never_through_master():
    spec = {"src_connection": "lfc_sqlserver_test", "kind": "list"}
    with pytest.raises(RuntimeError, match="FAILED_JDBC.CONNECTION"):
        d.list_databases(spec, query=_contained_user)       # the notebook records it on the master row
    typed = {"src_connection": "lfc_sqlserver_test", "databases": ["lfc_eng_001", "lfc_eng_002"]}
    assert not d.needs_listing(typed) and d.spec_errors(typed) == []
    assert [d.cfg_for(typed, db)["src_database"] for db in typed["databases"]] == ["lfc_eng_001", "lfc_eng_002"]


def test_a_server_login_lists_all_201_databases_by_name_and_the_filter_narrows_them_to_the_lab():
    spec = {"src_connection": "lfc_sqlserver_discover", "kind": "list"}
    assert d.needs_listing(spec) and len(d.list_databases(spec, query=_server_login)) == 201
    lab = d.list_databases(dict(spec, pattern="lfc_eng_%"), query=_server_login)
    assert lab == [f"lfc_eng_{i:03d}" for i in range(1, 101)]


def test_a_whole_server_discovery_lists_with_the_same_filter():
    spec = {"src_connection": "lfc_sqlserver_discover", "databases": "*", "pattern": "lfc_eng_%"}
    assert d.needs_listing(spec) and d.spec_errors(spec) == []
    assert len(d.list_databases(spec, query=_server_login)) == 100


def test_a_database_the_login_can_name_but_not_open_fails_on_its_own(monkeypatch):
    # discover_tables.ipynb reads each database in its own try (one()), recording a failure on that database's row
    # and going on: so the reading of a database raises, and the others are read as usual.
    def catalog(cfg, sql, *args):
        if cfg["src_database"] == "lfc_heavy_001":
            raise RuntimeError('Cannot open database "lfc_heavy_001" requested by the login. The login failed.')
        return []
    monkeypatch.setattr(d, "_query", catalog)
    spec = {"src_connection": "lfc_sqlserver_discover", "databases": ["lfc_eng_001", "lfc_heavy_001", "lfc_eng_002"]}
    read, failed = {}, {}
    for db in spec["databases"]:
        try:
            read[db] = d.discover_database(d.cfg_for(spec, db))
        except RuntimeError as e:
            failed[db] = str(e)
    assert sorted(read) == ["lfc_eng_001", "lfc_eng_002"] and list(failed) == ["lfc_heavy_001"]


# Listing an Azure SQL server's databases through Azure Resource Manager, with a Unity Catalog service credential:
# the credential and the HTTP call are faked; nothing reaches Azure.
SERVER_ID = "/subscriptions/0000-1111/resourceGroups/rg-lab/providers/Microsoft.Sql/servers/lab-sql"
LIST_URL = f"https://management.azure.com{SERVER_ID}/databases?api-version=2023-08-01"
THROUGH_AZURE = {"src_connection": "lfc_sqlserver_test", "kind": "list", "azure_credential": "lab_reader",
                 "azure_resource_id": SERVER_ID}


class _Credential:
    """dbutils.credentials.getServiceCredentialsProvider's provider, as azure-identity uses it."""
    def __init__(self):
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        return type("Token", (), {"token": f"token-{len(self.scopes)}"})()


def _azure(pages, calls=None):
    """Azure Resource Manager's list as pages, keyed by URL; each value is a page or an error to raise."""
    def get(url, token):
        if calls is not None:
            calls.append((url, token))
        answer = pages[url]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return get


def _refusal(code, body=b"", headers=None):
    import email.message
    import io
    import urllib.error
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(LIST_URL, code, "refused", message, io.BytesIO(body))


def _db(name, status="Online"):
    return {"name": name, "properties": {"status": status}}


def test_azure_lists_the_lab_servers_databases_page_by_page_with_a_token_for_each_page():
    second = LIST_URL + "&$skiptoken=page2"
    calls, credential = [], _Credential()
    get = _azure({LIST_URL: {"value": [_db("master")] + [_db(n) for n in LAB[:120]], "nextLink": second},
                  second: {"value": [_db(n) for n in LAB[120:]]}}, calls)
    assert d.needs_listing(THROUGH_AZURE) and d.lists_through_azure(THROUGH_AZURE)
    assert d.spec_errors(THROUGH_AZURE) == []
    names = d.list_databases(THROUGH_AZURE, query=lambda cfg, sql: pytest.fail("not through master"),
                             credential=credential, get=get)
    assert len(names) == 201 and "master" not in names and names == sorted(LAB)
    assert [url for url, _ in calls] == [LIST_URL, second] and [t for _, t in calls] == ["token-1", "token-2"]
    assert credential.scopes == ["https://management.azure.com/.default"] * 2


def test_the_filter_reads_as_like_does_on_the_names_azure_lists():
    get = _azure({LIST_URL: {"value": [_db(n) for n in LAB] + [_db("LFC_ENG_999")]}})
    spec = dict(THROUGH_AZURE, pattern="lfc_eng_%")
    assert d.list_databases(spec, credential=_Credential(), get=get) == [f"lfc_eng_{i:03d}" for i in range(1, 101)] + [
        "LFC_ENG_999"]
    get = _azure({LIST_URL: {"value": [_db(n) for n in LAB]}})
    assert d.list_databases(dict(spec, pattern="lfc_eng_0[0-1]_"), credential=_Credential(), get=get) == [
        f"lfc_eng_{i:03d}" for i in range(1, 20)]
    for pattern, name, matches in [("lfc_test", "lfc_test", True), ("lfc_test", "lfc_test_001", False),
                                   ("a.b", "axb", False), ("[[]x", "[x", True), ("a[b", "a[b", True)]:
        assert bool(d.like(pattern).match(name)) is matches, (pattern, name)


def test_system_databases_and_those_that_cant_be_opened_are_left_out():
    listed = [_db("master"), _db("eng_paused", "Paused"), _db("eng_new", "Creating"), _db("eng_off", "Offline"),
              {"name": "eng_plain"}, _db("eng_ok")]
    get = _azure({LIST_URL: {"value": listed}})
    assert d.list_databases(THROUGH_AZURE, credential=_Credential(), get=get) == ["eng_ok", "eng_paused", "eng_plain"]


def test_a_managed_instance_lists_the_same_way():
    instance = "/subscriptions/0000-1111/resourceGroups/rg-lab/providers/Microsoft.Sql/managedInstances/lab-mi"
    spec = dict(THROUGH_AZURE, azure_resource_id=instance)
    url = f"https://management.azure.com{instance}/databases?api-version=2023-08-01"
    assert d.spec_errors(spec) == []
    assert d.list_databases(spec, credential=_Credential(), get=_azure({url: {"value": [_db("sales")]}})) == ["sales"]


def test_azure_throttling_is_waited_out_and_a_refusal_says_what_to_grant():
    waits = []
    get = _azure({LIST_URL: [_refusal(429, headers={"Retry-After": "7"}), _refusal(503), {"value": [_db("eng")]}]})
    assert d.azure_databases(THROUGH_AZURE, _Credential(), get=get, sleep=waits.append) == ["eng"]
    assert waits == [7.0, 10.0]
    denied = _refusal(403, b'{"error": {"code": "AuthorizationFailed", "message": "no access"}}')
    with pytest.raises(RuntimeError, match=r"\(HTTP 403 AuthorizationFailed\): give the service credential's managed "
                                           r"identity \(its access connector's\) Reader on the server"):
        d.azure_databases(THROUGH_AZURE, _Credential(), get=_azure({LIST_URL: denied}), sleep=waits.append)
    with pytest.raises(RuntimeError, match="HTTP 404.*check the server's resource id"):
        d.azure_databases(THROUGH_AZURE, _Credential(), get=_azure({LIST_URL: _refusal(404)}))
    always = _azure({LIST_URL: [_refusal(500)] * 4})
    with pytest.raises(RuntimeError, match="HTTP 500"):
        d.azure_databases(THROUGH_AZURE, _Credential(), get=always, sleep=lambda s: None)


def test_the_token_never_follows_a_next_page_outside_azure_resource_manager():
    calls = []
    get = _azure({LIST_URL: {"value": [_db("eng")], "nextLink": "https://elsewhere.example.com/databases"}}, calls)
    with pytest.raises(RuntimeError, match="outside https://management.azure.com"):
        d.azure_databases(THROUGH_AZURE, _Credential(), get=get)
    assert [url for url, _ in calls] == [LIST_URL]


@pytest.mark.parametrize("change,error", [
    ({"azure_resource_id": None}, "needs both azure_credential"),
    ({"azure_credential": ""}, "needs both azure_credential"),
    ({"azure_credential": "lab reader"}, "azure_credential 'lab reader'"),
    ({"azure_resource_id": SERVER_ID + "/../../x"}, "azure_resource_id"),
    ({"azure_resource_id": SERVER_ID + "?api-version=1"}, "azure_resource_id"),
    ({"azure_resource_id": "/subscriptions/0000/resourceGroups/rg/providers/Microsoft.Web/sites/app"}, "resource_id"),
    ({"azure_resource_id": "https://evil.example.com" + SERVER_ID}, "azure_resource_id"),
])
def test_a_listing_through_azure_says_what_it_lacks(change, error):
    assert any(error in e for e in d.spec_errors(dict(THROUGH_AZURE, **change)))


def test_without_both_settings_the_server_is_listed_through_master_as_before():
    assert not d.lists_through_azure({"src_connection": "sql_a", "kind": "list"})
    listed = d.list_databases({"src_connection": "lfc_sqlserver_discover", "kind": "list"}, query=_server_login)
    assert listed == sorted(LAB)
    with pytest.raises(ValueError, match="service credential 'lab_reader'"):
        d.list_databases(THROUGH_AZURE)


def test_workers_are_the_requested_count_bounded_by_the_databases_and_the_source_cap():
    assert d.workers_for(d.DEFAULT_WORKERS, 22_000) == 32
    assert d.workers_for(32, 5) == 5
    assert d.workers_for(32, 22_000, max_queries_per_source=8) == 8
    assert d.workers_for(32, 22_000, max_queries_per_source=0) == 32
    assert d.workers_for(0, 10) == 1 and d.workers_for(32, 0) == 1
