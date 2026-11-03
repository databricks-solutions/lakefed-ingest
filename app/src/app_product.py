"""The product's name and version, so Databricks can attribute the operations app's usage.

The app's Databricks SDK client (``lakebase.workspace``) identifies itself with them, so each API call carries
``ingestion-workbench/<version>`` in its user agent, and the app's SQL warehouse statements carry ``QUERY_TAGS``.
Nothing else is collected: no names, hosts or data.

Kept in step with the engine's src/lakefed_ingest/product.py (tests/product_test.py pins the two together; this module is
app_product so it can't be confused with the engine's product module when both are on a path).
"""
PRODUCT_NAME = "ingestion-workbench"
PRODUCT_VERSION = "0.1.0"


def query_tags(component: str) -> list:
    """Query tags for a Statement Execution request: which product, and which part of it, sent the statement."""
    from databricks.sdk.service.sql import QueryTag
    return [QueryTag(key="application", value=PRODUCT_NAME), QueryTag(key="component", value=component)]
