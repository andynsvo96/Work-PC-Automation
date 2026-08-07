-- A scheduled auto clock-out may run on either desktop, but it should stay
-- with the computer that created it while that computer remains healthy.
alter table public.automation_queue
    add column if not exists preferred_node text;

create index if not exists automation_queue_preferred_node_idx
    on public.automation_queue (workspace_id, preferred_node, status, available_at);

create or replace function public.automation_enqueue_task(
    p_workspace_id uuid,
    p_label text,
    p_category text,
    p_task_type text,
    p_encrypted_payload text,
    p_requested_by_node text,
    p_requested_client_os text,
    p_target_node text,
    p_preferred_node text,
    p_required_capability text,
    p_details text,
    p_queue_mode text,
    p_available_at timestamptz,
    p_repeat_interval_minutes integer,
    p_app_commit text,
    p_protocol_version integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_control public.automation_queue_control%rowtype;
    v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    select * into v_control from public.automation_queue_control where workspace_id = p_workspace_id;
    if not found then raise exception 'Queue control is not configured'; end if;
    if v_control.paused then raise exception 'Queue paused: %', coalesce(v_control.pause_reason, 'manual review required'); end if;
    if p_protocol_version <> v_control.required_protocol_version then raise exception 'Queue protocol version mismatch'; end if;
    if v_control.required_commit is not null and p_app_commit <> v_control.required_commit then
        raise exception 'Strict version gate: app commit does not match';
    end if;
    if p_target_node is not null and not exists (
        select 1 from public.automation_nodes n
        where n.workspace_id = p_workspace_id and n.node_key = p_target_node and n.enabled
    ) then raise exception 'Selected target node is not registered or enabled'; end if;
    if p_preferred_node is not null and not exists (
        select 1 from public.automation_nodes n
        where n.workspace_id = p_workspace_id and n.node_key = p_preferred_node and n.enabled
    ) then raise exception 'Preferred node is not registered or enabled'; end if;

    insert into public.automation_queue (
        workspace_id, label, category, task_type, encrypted_payload,
        requested_by_node, requested_client_os, target_node, preferred_node, required_capability, details,
        queue_mode, available_at, repeat_interval_minutes, app_commit, protocol_version
    ) values (
        p_workspace_id, p_label, p_category, p_task_type, p_encrypted_payload,
        p_requested_by_node, coalesce(nullif(p_requested_client_os, ''), 'unknown'), p_target_node, p_preferred_node, p_required_capability, p_details,
        case when p_queue_mode in ('normal', 'scheduled', 'repeat') then p_queue_mode else 'normal' end,
        coalesce(p_available_at, now()), p_repeat_interval_minutes, p_app_commit, p_protocol_version
    ) returning * into v_task;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

create or replace function public.automation_claim_next_task(
    p_workspace_id uuid,
    p_node_key text,
    p_app_commit text,
    p_protocol_version integer,
    p_lease_seconds integer default 45
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_control public.automation_queue_control%rowtype;
    v_node public.automation_nodes%rowtype;
    v_task public.automation_queue%rowtype;
    v_stale_id uuid;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    select * into v_control from public.automation_queue_control where workspace_id = p_workspace_id for update;
    if v_control.paused then raise exception 'Queue paused: %', coalesce(v_control.pause_reason, 'manual review required'); end if;
    if p_protocol_version <> v_control.required_protocol_version then raise exception 'Queue protocol version mismatch'; end if;
    if v_control.required_commit is not null and p_app_commit <> v_control.required_commit then
        raise exception 'Strict version gate: app commit does not match';
    end if;
    select * into v_node from public.automation_nodes
        where workspace_id = p_workspace_id and node_key = p_node_key and enabled
        for update;
    if not found or v_node.last_seen_at < now() - interval '30 seconds' then
        raise exception 'Node heartbeat is stale or node is disabled';
    end if;
    if v_node.app_commit <> p_app_commit or v_node.protocol_version <> p_protocol_version then
        raise exception 'Node heartbeat version does not match claim request';
    end if;

    select id into v_stale_id from public.automation_queue
        where workspace_id = p_workspace_id and status = 'running' and lease_expires_at < now()
        order by sequence limit 1 for update;
    if v_stale_id is not null then
        update public.automation_queue set
            status = 'interrupted', success = false, completed_at = now(), updated_at = now(),
            message = 'Worker heartbeat was lost. Manual review is required; the task was not retried.'
        where id = v_stale_id;
        update public.automation_queue_control set
            paused = true,
            pause_reason = 'A running task lost its worker heartbeat. Review the CRM before resuming.',
            updated_at = now()
        where workspace_id = p_workspace_id;
        raise exception 'Queue paused after an interrupted task; manual review required';
    end if;
    if exists (select 1 from public.automation_queue where workspace_id = p_workspace_id and status = 'running') then
        return null;
    end if;

    select * into v_task from public.automation_queue q
        where q.workspace_id = p_workspace_id
          and q.status = 'queued'
          and q.available_at <= now()
          and (q.target_node is null or q.target_node = p_node_key)
          and (
              q.preferred_node is null
              or q.preferred_node = p_node_key
              or not exists (
                  select 1 from public.automation_nodes preferred
                  where preferred.workspace_id = p_workspace_id
                    and preferred.node_key = q.preferred_node
                    and preferred.enabled
                    and preferred.last_seen_at >= now() - interval '30 seconds'
              )
          )
        order by q.available_at, q.sequence limit 1 for update;
    if not found then return null; end if;
    if v_task.required_capability is not null
       and coalesce((v_node.capabilities ->> v_task.required_capability)::boolean, false) is not true then
        return null;
    end if;

    update public.automation_queue set
        status = 'running', claimed_by_node = p_node_key, lease_token = gen_random_uuid(),
        lease_expires_at = now() + make_interval(secs => greatest(30, least(120, p_lease_seconds))),
        attempt_count = attempt_count + 1, started_at = now(), updated_at = now(),
        message = 'Running on ' || p_node_key
    where id = v_task.id returning * into v_task;
    return to_jsonb(v_task);
end;
$$;

grant execute on function public.automation_enqueue_task(uuid,text,text,text,text,text,text,text,text,text,text,text,timestamptz,integer,text,integer) to authenticated;
grant execute on function public.automation_claim_next_task(uuid,text,text,integer,integer) to authenticated;
