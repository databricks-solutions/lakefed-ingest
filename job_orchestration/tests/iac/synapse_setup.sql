-- Create test table for partitioned ingestion testing
-- Run against the Synapse SQL pool (lakefed_ingest_test database)
CREATE TABLE dbo.sales_order
(
    order_id     INT           NOT NULL,
    customer_id  INT           NOT NULL,
    order_date   DATE          NOT NULL,
    total_amount DECIMAL(18,2),
    status       VARCHAR(50)
)
WITH
(
    DISTRIBUTION = HASH(order_id),
    CLUSTERED COLUMNSTORE INDEX
);
