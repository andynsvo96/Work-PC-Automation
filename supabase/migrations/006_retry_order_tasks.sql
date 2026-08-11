-- Requeue canceled or failed CRM work in the same queue row. Single-order
-- tasks preserve their encrypted arguments. Main Automation retries replace
-- the payload in place with a server-generated plan containing only failed
-- automation/order combinations.

drop function if exists public.automation_retry_order_task(uuid, uuid, jsonb);

create or replace function public.automation_retry_order_task(
    p_workspace_id uuid,
    p_task_id uuid,
    p_retry_context jsonb default '{}'::jsonb,
    p_encrypted_payload text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then
        raise exception 'Not a workspace member';
    end if;

    update public.automation_queue set
        encrypted_payload = coalesce(p_encrypted_payload, encrypted_payload),
        status = 'queued',
        available_at = now(),
        claimed_by_node = null,
        lease_token = null,
        lease_expires_at = null,
        cancel_requested = false,
        success = null,
        message = 'Retry waiting in queue.',
        started_at = null,
        completed_at = null,
        result_context = coalesce(p_retry_context, '{}'::jsonb),
        updated_at = now()
    where workspace_id = p_workspace_id
      and id = p_task_id
      and status in ('failed', 'canceled')
      and queue_mode = 'normal'
      and (
          (
              task_type in (
                  'crm.address_validator',
                  'crm.auto_splitter',
                  'crm.extension_order',
                  'crm.order_goods',
                  'crm.product_separator',
                  'crm.push_back',
                  'crm.sheet_scanner_order',
                  'crm.shipping_bypasser'
              )
              and label ~* 'Order[[:space:]]+[0-9]{7}([^0-9]|$)'
          )
          or (
              task_type = 'crm.processing'
              and jsonb_typeof(coalesce(p_retry_context, '{}'::jsonb) -> 'retry_plan') = 'object'
              and coalesce(p_retry_context -> 'retry_plan', '{}'::jsonb) <> '{}'::jsonb
              and p_encrypted_payload is not null
          )
      )
    returning * into v_task;

    if not found then
        raise exception 'Only canceled or failed CRM tasks with a safe retry scope can be retried';
    end if;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

grant execute on function public.automation_retry_order_task(uuid, uuid, jsonb, text) to authenticated;

notify pgrst, 'reload schema';
