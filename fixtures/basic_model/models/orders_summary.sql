-- Downstream of orders, so the DAG has an edge and column lineage has a parent.
select
    order_id,
    upper(status) as status_upper
from {{ ref('orders') }}
