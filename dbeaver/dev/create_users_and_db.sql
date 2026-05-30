-- Create Roles
CREATE ROLE footage_archive_dev_owner
  WITH LOGIN
  PASSWORD '${OWNER_USER_PASSWORD}';

CREATE ROLE footage_archive_dev_app
  WITH LOGIN
  PASSWORD '${APP_USER_PASSWORD}';

-- Create Database
CREATE DATABASE footage_archive_dev
  OWNER footage_archive_dev_owner;

-- Allow app user to connect
GRANT CONNECT ON DATABASE footage_archive_dev
  TO footage_archive_dev_app;