-- Auditoria somente leitura para PostgreSQL/Supabase. Execute com uma role administrativa.

select nspname as schema_name, nspowner::regrole as owner
from pg_namespace
where nspname not like 'pg_%' and nspname <> 'information_schema'
order by 1;

select n.nspname as schema_name, c.relname as table_name,
       c.relrowsecurity as rls_enabled, c.relforcerowsecurity as rls_forced
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
order by 1, 2;

select schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
from pg_policies
order by schemaname, tablename, policyname;

select table_schema, table_name, grantee, privilege_type
from information_schema.role_table_grants
where grantee in ('PUBLIC', 'anon', 'authenticated')
order by table_schema, table_name, grantee, privilege_type;

select routine_schema, routine_name, grantee, privilege_type
from information_schema.routine_privileges
where grantee in ('PUBLIC', 'anon', 'authenticated')
order by routine_schema, routine_name, grantee;

select n.nspname as schema_name, p.proname as function_name,
       pg_get_function_identity_arguments(p.oid) as arguments,
       p.prosecdef as security_definer, p.proconfig as function_config,
       p.proowner::regrole as owner,
       has_function_privilege('PUBLIC', p.oid, 'EXECUTE') as public_can_execute,
       has_function_privilege('anon', p.oid, 'EXECUTE') as anon_can_execute,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated_can_execute
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname not in ('pg_catalog', 'information_schema')
order by 1, 2;

select n.nspname as schema_name, c.relname as view_name,
       c.relowner::regrole as owner, c.reloptions,
       coalesce('security_invoker=true' = any(c.reloptions), false) as security_invoker,
       pg_get_viewdef(c.oid, true) as definition
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('v', 'm') and n.nspname not in ('pg_catalog', 'information_schema')
order by 1, 2;

select rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls
from pg_roles
where rolsuper or rolcreaterole or rolcreatedb or rolbypassrls
order by rolname;

select e.extname, e.extversion, n.nspname as schema_name, e.extowner::regrole as owner
from pg_extension e
join pg_namespace n on n.oid = e.extnamespace
order by e.extname;

select defaclrole::regrole as owner, coalesce(n.nspname, '*') as schema_name,
       defaclobjtype as object_type, defaclacl as default_acl
from pg_default_acl d
left join pg_namespace n on n.oid = d.defaclnamespace
order by 1, 2, 3;

select current_setting('pgrst.db_schemas', true) as postgrest_exposed_schemas,
       current_setting('pgrst.db_anon_role', true) as postgrest_anon_role;

select table_schema, table_name, column_name, grantee, privilege_type
from information_schema.column_privileges
where grantee in ('PUBLIC', 'anon', 'authenticated')
  and column_name in ('owner_id', 'user_id', 'status', 'role', 'is_admin')
order by table_schema, table_name, column_name, grantee;

-- Supabase Storage: consultas retornam vazio quando o schema/tabelas não existem.
select id, name, public, file_size_limit, allowed_mime_types
from storage.buckets
order by id;

select schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
from pg_policies
where schemaname = 'storage'
order by tablename, policyname;
