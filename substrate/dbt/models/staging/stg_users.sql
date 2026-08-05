-- Staging: raw.users -> typed, filtered.
--
-- This model selects raw.users.user_id BY NAME. When the reference-incident renames that
-- column to customer_id, this model fails to compile — a real dbt failure with a
-- real error message, which is the runtime evidence the Watcher picks up.
select
    user_id,
    email,
    country,
    signup_ts,
    is_active
from {{ source('raw', 'users') }}
where is_active
