-- SomnoAlert Supabase setup (idempotente)

create extension if not exists pgcrypto;

-- Sesiones de conduccion
create table if not exists public.sessions (
  session_id text primary key,
  vehicle_id text not null,
  driver_id text not null,
  start_time timestamptz not null default now(),
  end_time timestamptz,
  max_fatigue int not null default 0,
  alert_count int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Resumen por minuto
create table if not exists public.metrics_summary (
  id uuid primary key default gen_random_uuid(),
  session_id text references public.sessions(session_id) on delete set null,
  ts timestamptz not null default now(),
  avg_ear double precision,
  avg_mar double precision,
  avg_pitch double precision,
  perclos double precision,
  blink_freq double precision,
  fatigue_score int,
  fatigue_level int,
  fatigue_label text,
  illumination text,
  time_on_task int,
  monotony_index int,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Eventos detectados
create table if not exists public.events (
  id uuid primary key default gen_random_uuid(),
  session_id text references public.sessions(session_id) on delete set null,
  ts timestamptz not null default now(),
  event_type text not null,
  severity_level int,
  param_id text,
  param_value double precision,
  duration_ms int,
  fatigue_score int,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Alertas medicas / emergencia
create table if not exists public.emergency_alerts (
  id uuid primary key default gen_random_uuid(),
  session_id text references public.sessions(session_id) on delete set null,
  ts timestamptz not null default now(),
  emergency_type text not null,
  trigger_params jsonb not null default '{}'::jsonb,
  duration_seconds double precision,
  resolved_at timestamptz,
  resolution_type text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Telemetria cruda (para guardar exactamente el JSON que publica tu app)
create table if not exists public.telemetry_raw (
  id bigserial primary key,
  session_id text,
  ts timestamptz not null default now(),
  vehicle_id text,
  driver_id text,
  payload jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_sessions_vehicle_start on public.sessions(vehicle_id, start_time desc);
create index if not exists idx_metrics_session_ts on public.metrics_summary(session_id, ts desc);
create index if not exists idx_events_session_ts on public.events(session_id, ts desc);
create index if not exists idx_events_type_ts on public.events(event_type, ts desc);
create index if not exists idx_emergency_session_ts on public.emergency_alerts(session_id, ts desc);
create index if not exists idx_emergency_type_ts on public.emergency_alerts(emergency_type, ts desc);
create index if not exists idx_telemetry_raw_session_ts on public.telemetry_raw(session_id, ts desc);

-- Actualiza updated_at automaticamente en sessions
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sessions_updated_at on public.sessions;
create trigger trg_sessions_updated_at
before update on public.sessions
for each row
execute function public.set_updated_at();

-- RLS (lectura para usuarios autenticados; escritura recomendada via service_role)
alter table public.sessions enable row level security;
alter table public.metrics_summary enable row level security;
alter table public.events enable row level security;
alter table public.emergency_alerts enable row level security;
alter table public.telemetry_raw enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='sessions' and policyname='read_sessions_auth') then
    create policy read_sessions_auth on public.sessions for select to authenticated using (true);
  end if;

  if not exists (select 1 from pg_policies where schemaname='public' and tablename='metrics_summary' and policyname='read_metrics_auth') then
    create policy read_metrics_auth on public.metrics_summary for select to authenticated using (true);
  end if;

  if not exists (select 1 from pg_policies where schemaname='public' and tablename='events' and policyname='read_events_auth') then
    create policy read_events_auth on public.events for select to authenticated using (true);
  end if;

  if not exists (select 1 from pg_policies where schemaname='public' and tablename='emergency_alerts' and policyname='read_emergency_auth') then
    create policy read_emergency_auth on public.emergency_alerts for select to authenticated using (true);
  end if;

  if not exists (select 1 from pg_policies where schemaname='public' and tablename='telemetry_raw' and policyname='read_telemetry_raw_auth') then
    create policy read_telemetry_raw_auth on public.telemetry_raw for select to authenticated using (true);
  end if;
end $$;
