-- Runs only on a fresh PostgreSQL volume, never on an existing cluster.
\getenv app_user JAKH_DB_USER
\getenv app_database JAKH_DATABASE
\getenv app_password JAKH_INITIAL_APP_PASSWORD
CREATE ROLE :"app_user" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'app_password';
CREATE DATABASE :"app_database" OWNER :"app_user" TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE :"app_database" FROM PUBLIC;
\unset app_password
