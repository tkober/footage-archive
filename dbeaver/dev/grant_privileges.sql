ALTER SCHEMA public OWNER TO footage_archive_dev_owner;

GRANT USAGE ON SCHEMA public
  TO footage_archive_dev_app;

ALTER DEFAULT PRIVILEGES
  FOR ROLE footage_archive_dev_owner
  IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLES
  TO footage_archive_dev_app;

ALTER DEFAULT PRIVILEGES
  FOR ROLE footage_archive_dev_owner
  IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE
  ON SEQUENCES
  TO footage_archive_dev_app;