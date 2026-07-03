-- DealScout schema — paste into Supabase SQL Editor (Dashboard → SQL → New query)
-- The app uses the service-role key (server-side only), so RLS stays enabled
-- with no public policies: anon/authed clients can't read anything.

create table if not exists jobs (
    id uuid primary key default gen_random_uuid(),
    query text not null,
    max_price numeric,
    max_drive_min integer,
    include_seen boolean default false,
    status text not null default 'queued',   -- queued | running | done | error
    error text,
    progress text,
    created_at timestamptz default now(),
    started_at timestamptz,
    finished_at timestamptz
);

create table if not exists results (
    id bigint generated always as identity primary key,
    job_id uuid references jobs(id) on delete cascade,
    listing_id text,
    title text,
    price numeric,
    price_text text,
    location text,
    url text,
    image_url text,
    drive_minutes integer,
    score integer,
    verdict text,
    reason text,
    flags jsonb default '[]',
    seen_before boolean default false,
    created_at timestamptz default now()
);

create table if not exists settings (
    key text primary key,
    value jsonb
);

create table if not exists worker_heartbeat (
    id integer primary key,
    last_seen timestamptz
);

create index if not exists idx_jobs_status_created on jobs (status, created_at);
create index if not exists idx_results_job on results (job_id);

alter table jobs enable row level security;
alter table results enable row level security;
alter table settings enable row level security;
alter table worker_heartbeat enable row level security;
