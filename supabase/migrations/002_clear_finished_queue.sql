-- Adds the shared-queue Clear All operation without exposing direct table deletes.

create or replace function public.automation_clear_finished_tasks(p_workspace_id uuid)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_deleted integer := 0;
begin
    if not public.automation_is_member(p_workspace_id) then
        raise exception 'Not a workspace member';
    end if;

    delete from public.automation_queue
    where workspace_id = p_workspace_id
      and status in ('completed', 'failed', 'canceled', 'interrupted');
    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

grant execute on function public.automation_clear_finished_tasks(uuid) to authenticated;
