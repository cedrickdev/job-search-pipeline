from pipeline.jobs import dedup_hash, insert_job


def test_dedup_hash_ignores_case_punctuation_and_source():
    a = dedup_hash("Northwind", "Sales Assistant - Fresh Produce")
    b = dedup_hash("northwind", "sales assistant  fresh produce")
    assert a == b


def test_insert_job_dedups_across_sources(conn):
    job = {"company": "Globex", "title": "Senior Sales Assistant",
           "source": "wtj", "url": "https://wtj.example/1"}
    id1, created1 = insert_job(conn, job)
    id2, created2 = insert_job(conn, {**job, "source": "linkedin",
                                      "url": "https://linkedin.example/2"})
    assert created1 is True
    assert created2 is False
    assert id1 == id2
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1


def test_insert_job_backfills_missing_fields_on_dedup_hit(conn):
    first_id, _ = insert_job(conn, {"source": "migration", "company": "Acme",
                                    "title": "Sales Assistant"})
    job_id, created = insert_job(conn, {"source": "wtj", "company": "Acme",
                                        "title": "Sales Assistant",
                                        "url": "https://x.test/job",
                                        "description": "Build models."})
    assert (job_id, created) == (first_id, False)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["url"] == "https://x.test/job"
    assert row["description"] == "Build models."
    assert row["source"] == "migration"  # non-null fields are never overwritten
