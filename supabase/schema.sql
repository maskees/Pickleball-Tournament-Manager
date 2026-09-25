create extension if not exists "uuid-ossp";

create table if not exists public.tournaments (
  id uuid primary key default uuid_generate_v4(),
  name text not null,
  venue text not null,
  status text not null default 'UPCOMING' check (status in ('UPCOMING', 'LIVE', 'FINAL')),
  courts integer not null default 4 check (courts > 0),
  updated_at timestamptz not null default now()
);

create table if not exists public.matches (
  id uuid primary key default uuid_generate_v4(),
  tournament_id uuid not null references public.tournaments(id) on delete cascade,
  stage text not null default 'knockout' check (stage in ('group', 'knockout')),
  court text not null,
  round text not null,
  team_one text not null,
  team_two text not null,
  score_one integer not null default 0 check (score_one >= 0),
  score_two integer not null default 0 check (score_two >= 0),
  status text not null default 'UPCOMING' check (status in ('UPCOMING', 'LIVE', 'FINAL')),
  scheduled_at timestamptz not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.match_events (
  id bigint generated always as identity primary key,
  match_id uuid not null references public.matches(id) on delete cascade,
  actor uuid references auth.users(id),
  score_one integer not null check (score_one >= 0),
  score_two integer not null check (score_two >= 0),
  created_at timestamptz not null default now()
);

create table if not exists public.groups (
  id uuid primary key default uuid_generate_v4(),
  tournament_id uuid not null references public.tournaments(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

create table if not exists public.players (
  id uuid primary key default uuid_generate_v4(),
  group_id uuid not null references public.groups(id) on delete cascade,
  name text not null,
  seed integer not null,
  unique(group_id, name)
);

alter table public.tournaments add column if not exists courts integer not null default 4;
update public.tournaments set courts = 4 where courts is null;
alter table public.matches add column if not exists group_id uuid references public.groups(id) on delete set null;
alter table public.matches add column if not exists stage text not null default 'knockout';

alter table public.tournaments enable row level security;
alter table public.matches enable row level security;
alter table public.match_events enable row level security;
alter table public.groups enable row level security;
alter table public.players enable row level security;

drop policy if exists "Anyone can view tournaments" on public.tournaments;
drop policy if exists "Anyone can view matches" on public.matches;
drop policy if exists "Anyone can view match events" on public.match_events;
drop policy if exists "Directors can update tournaments" on public.tournaments;
drop policy if exists "Anyone can view groups" on public.groups;
drop policy if exists "Directors can manage groups" on public.groups;
drop policy if exists "Anyone can view players" on public.players;
drop policy if exists "Directors can manage players" on public.players;
drop policy if exists "Authenticated directors can update matches" on public.matches;
drop policy if exists "Authenticated directors can create match events" on public.match_events;

create policy "Anyone can view tournaments" on public.tournaments for select to anon, authenticated using (true);
create policy "Anyone can view matches" on public.matches for select to anon, authenticated using (true);
create policy "Anyone can view match events" on public.match_events for select to anon, authenticated using (true);

create policy "Directors can update tournaments" on public.tournaments for update to authenticated
using ((auth.jwt() ->> 'role') in ('admin', 'director'))
with check ((auth.jwt() ->> 'role') in ('admin', 'director'));
create policy "Anyone can view groups" on public.groups for select to anon, authenticated using (true);
create policy "Directors can manage groups" on public.groups for all to authenticated using ((auth.jwt() ->> 'role') in ('admin', 'director')) with check ((auth.jwt() ->> 'role') in ('admin', 'director'));
create policy "Anyone can view players" on public.players for select to anon, authenticated using (true);
create policy "Directors can manage players" on public.players for all to authenticated using ((auth.jwt() ->> 'role') in ('admin', 'director')) with check ((auth.jwt() ->> 'role') in ('admin', 'director'));

create policy "Authenticated directors can update matches" on public.matches for update to authenticated
using ((auth.jwt() ->> 'role') in ('admin', 'director'))
with check ((auth.jwt() ->> 'role') in ('admin', 'director'));

create policy "Authenticated directors can create match events" on public.match_events for insert to authenticated
with check ((auth.jwt() ->> 'role') in ('admin', 'director'));

do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'matches') then
    alter publication supabase_realtime add table public.matches;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'match_events') then
    alter publication supabase_realtime add table public.match_events;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'groups') then
    alter publication supabase_realtime add table public.groups;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'players') then
    alter publication supabase_realtime add table public.players;
  end if;
end $$;
