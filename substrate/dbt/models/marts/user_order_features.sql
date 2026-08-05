-- The ML feature table. This is the terminus the blast radius must reach:
-- substrate/ml/train_churn_model.py trains on exactly this table, and the model
-- is registered in DataHub as an mlModel with lineage back to here.
--
-- the design thesis: "Makes the Production ML Agents claim factual, not aspirational."
select
    u.user_id,
    u.country,
    count(o.order_id)                                        as order_count,
    coalesce(sum(o.amount_cents), 0)                         as lifetime_value_cents,
    coalesce(avg(o.amount_cents), 0)                         as avg_order_cents,
    sum(case when o.status = 'refunded' then 1 else 0 end)   as refund_count,
    max(o.ordered_at)                                        as last_order_at
from {{ ref('stg_users') }} u
left join {{ ref('stg_orders') }} o
    on o.user_id = u.user_id
group by u.user_id, u.country
