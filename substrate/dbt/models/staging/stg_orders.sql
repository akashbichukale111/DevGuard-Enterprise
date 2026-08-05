select
    order_id,
    user_id,
    amount_cents,
    status,
    ordered_at
from {{ source('raw', 'orders') }}
