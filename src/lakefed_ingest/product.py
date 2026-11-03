"""The product's name and version, so Databricks can attribute its usage.

Every Databricks SDK client the engine makes is built by ``workspace_client``, so each API call carries
``ingestion-workbench/<version>`` in its user agent. The bundle's resources carry the same name as their
``project`` tag (the ``project_tag`` bundle variable). Nothing else is collected: no names, hosts or data.

Keep PRODUCT_VERSION in step with app/src/app_product.py (tests/product_test.py pins the two together).
"""
PRODUCT_NAME = "ingestion-workbench"
PRODUCT_VERSION = "0.1.0"


def workspace_client(**kwargs):
    """A Databricks SDK ``WorkspaceClient`` that identifies itself as this product."""
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(product=PRODUCT_NAME, product_version=PRODUCT_VERSION, **kwargs)
