-- Reuse a failed queue entry for an in-place, auditable retry.

alter table public.automation_queue
    add column if not exists result_context jsonb not null default '{}'::jsonb;

drop function if exists public.automation_finish_task(uuid, uuid, text, uuid, boolean, text);

create or replace function public.automation_finish_task(
    p_workspace_id uuid, p_task_id uuid, p_node_key text, p_lease_token uuid,
    p_success boolean, p_message text, p_result_context jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    select * into v_task from public.automation_queue
    where workspace_id = p_workspace_id and id = p_task_id and status = 'running'
      and claimed_by_node = p_node_key and lease_token = p_lease_token
    for update;
    if not found then raise exception 'Task lease is no longer valid'; end if;
    insert into public.automation_queue_runs(
        workspace_id, task_id, run_number, claimed_by_node, started_at, success, message
    ) values (
        p_workspace_id, p_task_id, v_task.attempt_count, p_node_key, v_task.started_at,
        p_success, coalesce(nullif(p_message, ''), 'Task finished.')
    );
    update public.automation_queue set
        status = case
            when cancel_requested then 'canceled'
            when queue_mode = 'repeat' then 'queued'
            when p_success then 'completed'
            else 'failed'
        end,
        success = case when cancel_requested then false else p_success end,
        message = case when queue_mode = 'repeat' and not cancel_requested
            then 'Idle until the next repeat run.'
            else coalesce(nullif(p_message, ''), 'Task finished.') end,
        available_at = case when queue_mode = 'repeat' and not cancel_requested
            then now() + make_interval(mins => greatest(0, coalesce(repeat_interval_minutes, 5)))
            else available_at end,
        completed_at = case when queue_mode = 'repeat' and not cancel_requested then null else now() end,
        started_at = case when queue_mode = 'repeat' and not cancel_requested then null else started_at end,
        claimed_by_node = case when queue_mode = 'repeat' and not cancel_requested then null else claimed_by_node end,
        lease_token = null,
        lease_expires_at = null,
        result_context = coalesce(p_result_context, result_context, '{}'::jsonb),
        updated_at = now()
    where workspace_id = p_workspace_id and id = p_task_id and status = 'running'
      and claimed_by_node = p_node_key and lease_token = p_lease_token
    returning * into v_task;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

create or replace function public.automation_retry_failed_task(
    p_workspace_id uuid,
    p_task_id uuid,
    p_encrypted_payload text,
    p_retry_context jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    update public.automation_queue set
        encrypted_payload = p_encrypted_payload,
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
      and status = 'failed'
      and task_type = 'crm.mass_emailer'
      and label = 'Sheets Scanner'
      and queue_mode = 'normal'
    returning * into v_task;
    if not found then raise exception 'Only a failed live Sheets Scanner task can be retried in place'; end if;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

grant execute on function public.automation_finish_task(uuid, uuid, text, uuid, boolean, text, jsonb) to authenticated;
grant execute on function public.automation_retry_failed_task(uuid, uuid, text, jsonb) to authenticated;
