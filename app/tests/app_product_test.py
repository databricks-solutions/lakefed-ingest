"""The app's product identity: its SDK client and its SQL warehouse statements name the product."""
import app_product as product


def test_query_tags_name_the_product_and_the_part_that_sent_the_statement():
    tags = product.query_tags("discovery")
    assert [(t.key, t.value) for t in tags] == [("application", "ingestion-workbench"), ("component", "discovery")]


def test_the_apps_sdk_client_identifies_itself(monkeypatch):
    import lakebase
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    monkeypatch.setattr(lakebase, "WorkspaceClient", FakeClient)
    lakebase._new_client()        # what workspace() builds (the tests' guard stands in for workspace itself)
    assert seen == {"product": "ingestion-workbench", "product_version": product.PRODUCT_VERSION}
