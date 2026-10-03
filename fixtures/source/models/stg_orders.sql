select order_id, _loaded_at
from {{ source('raw', 'raw_orders') }}
