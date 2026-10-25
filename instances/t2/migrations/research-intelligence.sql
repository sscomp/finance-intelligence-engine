-- Additive; target and role must be independently verified.
CREATE SCHEMA IF NOT EXISTS t2_research;
REVOKE ALL ON SCHEMA t2_research FROM PUBLIC;
GRANT USAGE ON SCHEMA t2_research TO fie_t2_ingestor;
CREATE TABLE IF NOT EXISTS t2_research.versions(source text NOT NULL,record_key text NOT NULL,version text NOT NULL,payload jsonb NOT NULL,first_batch text NOT NULL,first_seen timestamptz NOT NULL,PRIMARY KEY(source,record_key,version));
CREATE TABLE IF NOT EXISTS t2_research.heads(source text NOT NULL,record_key text NOT NULL,version text NOT NULL,observed_at timestamptz NOT NULL,PRIMARY KEY(source,record_key),FOREIGN KEY(source,record_key,version) REFERENCES t2_research.versions(source,record_key,version));
CREATE TABLE IF NOT EXISTS t2_research.receipts(batch_id text PRIMARY KEY,source text NOT NULL,committed_at timestamptz NOT NULL,rows integer NOT NULL,coverage jsonb NOT NULL,failures jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS t2_research.reports(report_id text PRIMARY KEY,job text NOT NULL,generated_at text NOT NULL,payload jsonb NOT NULL);
GRANT SELECT,INSERT ON t2_research.versions,t2_research.heads,t2_research.receipts,t2_research.reports TO fie_t2_ingestor;
GRANT UPDATE(version,observed_at) ON t2_research.heads TO fie_t2_ingestor;
