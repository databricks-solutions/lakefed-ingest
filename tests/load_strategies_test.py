"""Load strategies: registry, support matrix, CT position encoding, FULL load, placeholders."""
import pytest

import ct_engine as ce
import load_strategies as ls


def test_registry_and_default():
    assert ls.LOAD_TYPES == ("ct", "cdc", "cursor", "full")
    assert ls.get_strategy(None).name == "ct"                     # the column default
    assert ls.get_strategy(" FULL ").name == "full"
    with pytest.raises(ValueError, match="unsupported load_type 'incremental'"):
        ls.get_strategy("incremental")


def test_support_matrix():
    assert ls.support_matrix() == {
        "sqlserver":  {"ct": "yes", "cdc": "placeholder", "cursor": "yes",         "full": "yes"},
        "oracle":     {"ct": "no",  "cdc": "placeholder", "cursor": "placeholder", "full": "yes"},
        "postgresql": {"ct": "no",  "cdc": "placeholder", "cursor": "placeholder", "full": "yes"},
        "mysql":      {"ct": "no",  "cdc": "placeholder", "cursor": "placeholder", "full": "yes"},
    }


def test_unsupported_reasons_are_clear():
    assert ls.supported("sqlserver", "ct") and ls.supported(None, None) and ls.supported("mysql", "full")
    assert ls.unsupported_reason("oracle", "ct") == \
        "load_type 'ct' is not supported for src_type 'oracle' (supported: full)"
    assert ls.unsupported_reason("postgresql", "cdc") == \
        "load_type 'cdc' is not implemented yet (src_type 'postgresql')"
    assert "unsupported src_type 'db2'" in ls.unsupported_reason("db2", "full")
    assert "unsupported load_type 'weekly'" in ls.unsupported_reason("sqlserver", "weekly")


def test_ct_position_round_trip():
    ct = ls.get_strategy("ct")
    assert ct.encode_position(287493) == "287493" and ct.decode_position("287493") == 287493
    assert ct.encode_position(None) is None
    assert ct.decode_position(None) == 0 and ct.decode_position("") == 0
    # text positions must never compare lexically
    assert ct.decode_position("10") > ct.decode_position("9")


def test_pk_requirements():
    assert ls.get_strategy("ct").requires_pk and ls.get_strategy("cursor").requires_pk
    assert ls.get_strategy("cdc").requires_pk and not ls.get_strategy("full").requires_pk


def test_placeholders_raise_not_implemented():
    s = ls.get_strategy("cdc")
    assert not s.implemented and "PLACEHOLDER" in type(s).__doc__
    with pytest.raises(NotImplementedError, match="'cdc' is not implemented"):
        s.load(None, {}, "c.s.t")


def test_cursor_is_sqlserver_only_with_placeholders_elsewhere():
    assert ls.supported("sqlserver", "cursor") and ls.get_strategy("cursor").implemented
    for st in ("oracle", "postgresql", "mysql"):
        assert ls.unsupported_reason(st, "cursor") == \
            f"load_type 'cursor' is not implemented yet (src_type '{st}')"


def test_cursor_position_round_trip():
    import cursor_engine as cur
    s = ls.get_strategy("cursor")
    text = cur.encode_position("rv", "timestamp", "2001")
    assert s.decode_position(s.encode_position(text)) == {"col": "rv", "type": "timestamp", "value": "2001"}
    assert s.decode_position(None) is None and s.decode_position("287493") is None


def test_full_load_ensures_sink_then_seeds(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "ensure_sink_table", lambda sp, cfg, sink, slots=None: calls.append(("ensure", sink)))
    monkeypatch.setattr(ce, "seed", lambda sp, cfg, sink, slots=None, executor=None, guard=None:
                        calls.append(("seed", sink, guard)) or 42)
    g = object()
    assert ls.get_strategy("full").load("spark", {"id": 1}, "c.s.t", guard=g) == 42
    assert calls == [("ensure", "c.s.t"), ("seed", "c.s.t", g)]
