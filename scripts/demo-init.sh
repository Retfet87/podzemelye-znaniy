#!/bin/sh
set -eu
# Run by postgres entrypoint only in this new dedicated demo volume.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname podzemelye_demo \
    --set=app_password="$DEMO_DB_PASSWORD" <<'SQL'
CREATE ROLE demo_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE podzemelye_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE podzemelye_demo TO demo_app;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO demo_app;
CREATE TABLE public.demo_environment (environment text PRIMARY KEY);
INSERT INTO public.demo_environment VALUES ('podzemelye-demo-v1');
GRANT SELECT ON public.demo_environment TO demo_app;
SQL
