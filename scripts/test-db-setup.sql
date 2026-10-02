-- Roles and database for the test suite (local throwaway PostgreSQL or the CI service container).
-- Run as a superuser against a server that trusts test connections, so no password appears here.
-- Neither role is a superuser or bypasses row-level security; the app role inherits nothing, so it
-- can never act as the owner.
CREATE ROLE asset_shells_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
CREATE ROLE asset_shells_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOINHERIT;
CREATE DATABASE asset_shells_test OWNER asset_shells_owner;
