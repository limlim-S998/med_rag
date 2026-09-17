-- Workers now validate writer membership themselves as well as at NGINX.
-- Airflow's workload has no SQL grants; it can only coordinate admitted batches.
GRANT SELECT ON core.study_access TO [id-medw-ingestion];
ALTER TABLE audit.index_event ADD job_id VARCHAR(64) NULL;
ALTER TABLE audit.index_event ADD batch_id CHAR(64) NULL;
ALTER TABLE audit.index_event ADD requested_by_oid VARCHAR(64) NULL;
GO
