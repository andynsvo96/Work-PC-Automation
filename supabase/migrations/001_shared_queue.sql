-- Shared, strictly serialized automation queue for Windows and macOS nodes.
-- Run this migration in the Supabase SQL editor, then create one Auth user per
-- computer and add both users to the same automation workspace.

create extension if not exists pgcrypto;

create table if not exists public.automation_workspaces (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    created_by uuid not null default auth.uid(),
    created_at timestamptz not null default now()
);

create table if not exists public.automation_workspace_members (
    workspace_id uuid not null references public.automation_workspaces(id) on delete cascade,
    user_id uuid not null,
    role text not null default 'operator' check (role in ('owner', 'operator', 'viewer')),
    created_at timestamptz not null default now(),
    primary key (workspace_id, user_id)
);

create table if not exists public.automation_nodes (
    workspace_id uuid not null references public.automation_workspaces(id) on delete cascade,
    node_key text not null,
    display_name text not null,
    os_name text not null,
    architecture text,
    app_commit text not null,
    protocol_version integer not null,
    capabilities jsonb not null default '{}'::jsonb,
    runtime_status jsonb not null default '{}'::jsonb,
    last_seen_at timestamptz not null default now(),
    enabled boolean not null default true,
    primary key (workspace_id, node_key)
);

create table if not exists public.automation_queue_control (
    workspace_id uuid primary key references public.automation_workspaces(id) on delete cascade,
    required_commit text,
    required_protocol_version integer not null default 1,
    paused boolean not null default false,
    pause_reason text,
    updated_at timestamptz not null default now()
);

create table if not exists public.automation_queue (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.automation_workspaces(id) on delete cascade,
    sequence bigint generated always as identity,
    label text not null,
    category text not null default 'Automation',
    task_type text not null,
    encrypted_payload text not null,
    requested_by_node text not null,
    requested_client_os text not null default 'unknown',
    target_node text,
    required_capability text,
    details text,
    queue_mode text not null default 'normal' check (queue_mode in ('normal', 'scheduled', 'repeat')),
    repeat_interval_minutes integer,
    available_at timestamptz not null default now(),
    status text not null default 'queued' check (
        status in ('queued', 'running', 'completed', 'failed', 'canceled', 'interrupted')
    ),
    app_commit text not null,
    protocol_version integer not null,
    claimed_by_node text,
    lease_token uuid,
    lease_expires_at timestamptz,
    cancel_requested boolean not null default false,
    attempt_count integer not null default 0,
    success boolean,
    message text not null default 'Waiting in queue.',
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz not null default now(),
    unique (workspace_id, sequence)
);

create index if not exists automation_queue_claim_idx
    on public.automation_queue (workspace_id, status, available_at, sequence);
create index if not exists automation_queue_history_idx
    on public.automation_queue (workspace_id, completed_at desc);

create table if not exists public.automation_queue_runs (
    id bigint generated always as identity primary key,
    workspace_id uuid not null references public.automation_workspaces(id) on delete cascade,
    task_id uuid not null references public.automation_queue(id) on delete cascade,
    run_number integer not null,
    claimed_by_node text not null,
    started_at timestamptz,
    completed_at timestamptz not null default now(),
    success boolean not null,
    message text not null
);
create index if not exists automation_queue_runs_task_idx
    on public.automation_queue_runs (workspace_id, task_id, completed_at desc);

alter table public.automation_workspaces enable row level security;
alter table public.automation_workspace_members enable row level security;
alter table public.automation_nodes enable row level security;
alter table public.automation_queue_control enable row level security;
alter table public.automation_queue enable row level security;
alter table public.automation_queue_runs enable row level security;

create or replace function public.automation_is_member(p_workspace_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from public.automation_workspace_members m
        where m.workspace_id = p_workspace_id and m.user_id = auth.uid()
    );
$$;

drop policy if exists automation_workspaces_read on public.automation_workspaces;
create policy automation_workspaces_read on public.automation_workspaces
    for select using (public.automation_is_member(id));
drop policy if exists automation_members_read on public.automation_workspace_members;
create policy automation_members_read on public.automation_workspace_members
    for select using (public.automation_is_member(workspace_id));
drop policy if exists automation_nodes_read on public.automation_nodes;
create policy automation_nodes_read on public.automation_nodes
    for select using (public.automation_is_member(workspace_id));
drop policy if exists automation_control_read on public.automation_queue_control;
create policy automation_control_read on public.automation_queue_control
    for select using (public.automation_is_member(workspace_id));
drop policy if exists automation_queue_read on public.automation_queue;
create policy automation_queue_read on public.automation_queue
    for select using (public.automation_is_member(workspace_id));
drop policy if exists automation_queue_runs_read on public.automation_queue_runs;
create policy automation_queue_runs_read on public.automation_queue_runs
    for select using (public.automation_is_member(workspace_id));

create or replace function public.automation_create_workspace(p_name text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_workspace_id uuid;
begin
    if auth.uid() is null then
        raise exception 'Authentication required';
    end if;
    insert into public.automation_workspaces(name, created_by)
    values (coalesce(nullif(trim(p_name), ''), 'Automation'), auth.uid())
    returning id into v_workspace_id;
    insert into public.automation_workspace_members(workspace_id, user_id, role)
    values (v_workspace_id, auth.uid(), 'owner');
    insert into public.automation_queue_control(workspace_id)
    values (v_workspace_id);
    return v_workspace_id;
end;
$$;

create or replace function public.automation_node_heartbeat(
    p_workspace_id uuid,
    p_node_key text,
    p_display_name text,
    p_os_name text,
    p_architecture text,
    p_app_commit text,
    p_protocol_version integer,
    p_capabilities jsonb,
    p_runtime_status jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_control public.automation_queue_control%rowtype;
    v_eligible boolean;
begin
    if not public.automation_is_member(p_workspace_id) then
        raise exception 'Not a workspace member';
    end if;
    insert into public.automation_nodes(
        workspace_id, node_key, display_name, os_name, architecture,
        app_commit, protocol_version, capabilities, runtime_status, last_seen_at
    ) values (
        p_workspace_id, p_node_key, p_display_name, p_os_name, p_architecture,
        p_app_commit, p_protocol_version, coalesce(p_capabilities, '{}'::jsonb),
        coalesce(p_runtime_status, '{}'::jsonb), now()
    )
    on conflict (workspace_id, node_key) do update set
        display_name = excluded.display_name,
        os_name = excluded.os_name,
        architecture = excluded.architecture,
        app_commit = excluded.app_commit,
        protocol_version = excluded.protocol_version,
        capabilities = excluded.capabilities,
        runtime_status = excluded.runtime_status,
        last_seen_at = now();

    insert into public.automation_queue_control(workspace_id)
    values (p_workspace_id) on conflict do nothing;
    select * into v_control from public.automation_queue_control where workspace_id = p_workspace_id;
    v_eligible := not v_control.paused
        and p_protocol_version = v_control.required_protocol_version
        and (v_control.required_commit is null or p_app_commit = v_control.required_commit);
    return jsonb_build_object(
        'ok', true,
        'eligible', v_eligible,
        'paused', v_control.paused,
        'pause_reason', v_control.pause_reason,
        'required_commit', v_control.required_commit,
        'required_protocol_version', v_control.required_protocol_version,
        'server_time', now()
    );
end;
$$;

create or replace function public.automation_add_workspace_member(
    p_workspace_id uuid, p_user_id uuid, p_role text default 'operator'
)
returns jsonb language plpgsql security definer set search_path = public as $$
begin
    if not exists (
        select 1 from public.automation_workspace_members
        where workspace_id = p_workspace_id and user_id = auth.uid() and role = 'owner'
    ) then raise exception 'Workspace owner permission required'; end if;
    insert into public.automation_workspace_members(workspace_id, user_id, role)
    values (
        p_workspace_id,
        p_user_id,
        case when p_role in ('owner', 'operator', 'viewer') then p_role else 'operator' end
    ) on conflict (workspace_id, user_id) do update set role = excluded.role;
    return jsonb_build_object('ok', true, 'user_id', p_user_id);
end;
$$;

create or replace function public.automation_set_version_gate(
    p_workspace_id uuid, p_required_commit text, p_required_protocol_version integer default 1
)
returns jsonb language plpgsql security definer set search_path = public as $$
begin
    if not exists (
        select 1 from public.automation_workspace_members
        where workspace_id = p_workspace_id and user_id = auth.uid() and role = 'owner'
    ) then raise exception 'Workspace owner permission required'; end if;
    update public.automation_queue_control set
        required_commit = nullif(trim(p_required_commit), ''),
        required_protocol_version = p_required_protocol_version,
        updated_at = now()
    where workspace_id = p_workspace_id;
    return jsonb_build_object('ok', true, 'required_commit', nullif(trim(p_required_commit), ''));
end;
$$;

create or replace function public.automation_resume_queue(
    p_workspace_id uuid, p_review_note text
)
returns jsonb language plpgsql security definer set search_path = public as $$
begin
    if not exists (
        select 1 from public.automation_workspace_members
        where workspace_id = p_workspace_id and user_id = auth.uid() and role in ('owner', 'operator')
    ) then raise exception 'Operator permission required'; end if;
    if length(trim(coalesce(p_review_note, ''))) < 5 then
        raise exception 'A manual review note is required before resuming';
    end if;
    update public.automation_queue_control set
        paused = false,
        pause_reason = 'Resumed after manual review: ' || trim(p_review_note),
        updated_at = now()
    where workspace_id = p_workspace_id;
    return jsonb_build_object('ok', true, 'message', 'Queue resumed after manual review.');
end;
$$;

create or replace function public.automation_enqueue_task(
    p_workspace_id uuid,
    p_label text,
    p_category text,
    p_task_type text,
    p_encrypted_payload text,
    p_requested_by_node text,
    p_requested_client_os text,
    p_target_node text,
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
    ) then raise exception 'Target node is not registered'; end if;

    insert into public.automation_queue(
        workspace_id, label, category, task_type, encrypted_payload,
        requested_by_node, requested_client_os, target_node, required_capability, details,
        queue_mode, available_at, repeat_interval_minutes, app_commit, protocol_version
    ) values (
        p_workspace_id, p_label, p_category, p_task_type, p_encrypted_payload,
        p_requested_by_node, coalesce(nullif(p_requested_client_os, ''), 'unknown'),
        p_target_node, p_required_capability, p_details,
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

    select * into v_task from public.automation_queue
        where workspace_id = p_workspace_id and status = 'queued' and available_at <= now()
        order by available_at, sequence limit 1 for update;
    if not found then return null; end if;
    if v_task.target_node is not null and v_task.target_node <> p_node_key then return null; end if;
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

create or replace function public.automation_renew_task_lease(
    p_workspace_id uuid, p_task_id uuid, p_node_key text, p_lease_token uuid,
    p_lease_seconds integer default 45
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
        lease_expires_at = now() + make_interval(secs => greatest(30, least(120, p_lease_seconds))),
        updated_at = now()
    where workspace_id = p_workspace_id and id = p_task_id and status = 'running'
      and claimed_by_node = p_node_key and lease_token = p_lease_token
    returning * into v_task;
    if not found then raise exception 'Task lease is no longer valid'; end if;
    return jsonb_build_object('ok', true, 'cancel_requested', v_task.cancel_requested, 'lease_expires_at', v_task.lease_expires_at);
end;
$$;

create or replace function public.automation_finish_task(
    p_workspace_id uuid, p_task_id uuid, p_node_key text, p_lease_token uuid,
    p_success boolean, p_message text
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
            then now() + make_interval(mins => greatest(5, least(60, coalesce(repeat_interval_minutes, 5))))
            else available_at end,
        completed_at = case when queue_mode = 'repeat' and not cancel_requested then null else now() end,
        started_at = case when queue_mode = 'repeat' and not cancel_requested then null else started_at end,
        claimed_by_node = case when queue_mode = 'repeat' and not cancel_requested then null else claimed_by_node end,
        lease_token = null, lease_expires_at = null, updated_at = now()
    where workspace_id = p_workspace_id and id = p_task_id and status = 'running'
      and claimed_by_node = p_node_key and lease_token = p_lease_token
    returning * into v_task;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

create or replace function public.automation_cancel_task(p_workspace_id uuid, p_task_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    update public.automation_queue set
        status = case when status = 'queued' then 'canceled' else status end,
        cancel_requested = case when status = 'running' then true else cancel_requested end,
        success = case when status = 'queued' then false else success end,
        completed_at = case when status = 'queued' then now() else completed_at end,
        message = case when status = 'running' then 'Cancel requested by user.' else 'Canceled before start.' end,
        updated_at = now()
    where workspace_id = p_workspace_id and id = p_task_id and status in ('queued', 'running')
    returning * into v_task;
    if not found then raise exception 'Only queued or running tasks can be canceled'; end if;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

create or replace function public.automation_reassign_task(
    p_workspace_id uuid, p_task_id uuid, p_target_node text
)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_task public.automation_queue%rowtype;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    if p_target_node is not null and not exists (
        select 1 from public.automation_nodes where workspace_id = p_workspace_id
          and node_key = p_target_node and enabled
    ) then raise exception 'Target node is not registered'; end if;
    update public.automation_queue set target_node = p_target_node, updated_at = now(),
        message = case when p_target_node is null then 'Waiting for the next eligible computer.'
                       else 'Waiting for ' || p_target_node || '.' end
    where workspace_id = p_workspace_id and id = p_task_id and status = 'queued'
    returning * into v_task;
    if not found then raise exception 'Only a queued task can be safely reassigned'; end if;
    return to_jsonb(v_task) - 'encrypted_payload';
end;
$$;

create or replace function public.automation_queue_snapshot(p_workspace_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_rows jsonb;
begin
    if not public.automation_is_member(p_workspace_id) then raise exception 'Not a workspace member'; end if;
    select coalesce(jsonb_agg(to_jsonb(q) - 'encrypted_payload' order by q.available_at, q.sequence), '[]'::jsonb)
    into v_rows
    from (
        select * from public.automation_queue
        where workspace_id = p_workspace_id
        order by available_at, sequence
        limit 100
    ) q;
    return v_rows;
end;
$$;

grant execute on function public.automation_create_workspace(text) to authenticated;
grant execute on function public.automation_node_heartbeat(uuid,text,text,text,text,text,integer,jsonb,jsonb) to authenticated;
grant execute on function public.automation_add_workspace_member(uuid,uuid,text) to authenticated;
grant execute on function public.automation_set_version_gate(uuid,text,integer) to authenticated;
grant execute on function public.automation_resume_queue(uuid,text) to authenticated;
grant execute on function public.automation_enqueue_task(uuid,text,text,text,text,text,text,text,text,text,text,timestamptz,integer,text,integer) to authenticated;
grant execute on function public.automation_claim_next_task(uuid,text,text,integer,integer) to authenticated;
grant execute on function public.automation_renew_task_lease(uuid,uuid,text,uuid,integer) to authenticated;
grant execute on function public.automation_finish_task(uuid,uuid,text,uuid,boolean,text) to authenticated;
grant execute on function public.automation_cancel_task(uuid,uuid) to authenticated;
grant execute on function public.automation_reassign_task(uuid,uuid,text) to authenticated;
grant execute on function public.automation_queue_snapshot(uuid) to authenticated;
grant select on public.automation_workspaces, public.automation_workspace_members,
    public.automation_nodes, public.automation_queue_control to authenticated;

revoke select on public.automation_queue, public.automation_queue_runs from authenticated;

revoke insert, update, delete on public.automation_nodes, public.automation_queue_control,
    public.automation_queue, public.automation_queue_runs from authenticated;
