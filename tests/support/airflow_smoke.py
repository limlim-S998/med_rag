"""Run the packaged DAG with real Airflow and loopback identity/API fixtures.

Executed only inside the disposable Airflow image by scripts/smoke_image.py.
The production DAG and workload-identity client are imported without patches.
"""

import hashlib
import ipaddress
import json
import os
import ssl
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    (path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (path / "assertion").write_text("offline-federated-assertion")
    selections = {}
    outcomes = {"status": "completed"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/oauth2/v2.0/token"):
                assert b"client_assertion=offline-federated-assertion" in body
                self.reply({"access_token": "offline-access-token", "expires_in": 3600, "token_type": "Bearer"})
                return
            assert self.headers["Authorization"] == "Bearer offline-access-token"
            assert self.path == "/_internal/batches"
            request = json.loads(body)
            batch_id = hashlib.sha256(("ingest_study/" + request["run_id"]).encode()).hexdigest()
            selections[batch_id] = request
            self.reply({"id": batch_id})

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer offline-access-token"
            batch_id = self.path.rsplit("/", 1)[-1]
            assert batch_id in selections
            self.reply({"id": batch_id, "status": outcomes["status"], "deferred_by_limit": 0,
                        "counts": {"done": 2 if outcomes["status"] == "completed" else 1,
                                   "failed": 1 if outcomes["status"] == "failed" else 0}})

    identity = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(path / "cert.pem", path / "key.pem")
    identity.socket = context.wrap_socket(identity.socket, server_side=True)
    api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    for server in (identity, api):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update({
        "AZURE_TENANT_ID": "offline-tenant", "AZURE_CLIENT_ID": "offline-client",
        "AZURE_FEDERATED_TOKEN_FILE": str(path / "assertion"),
        "AZURE_AUTHORITY_HOST": f"https://localhost:{identity.server_port}",
        "REQUESTS_CA_BUNDLE": str(path / "cert.pem"),
        "MEDW_AUTH_AUDIENCE": "offline-api", "MEDW_INGESTION_URL": f"http://127.0.0.1:{api.server_port}",
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION": "False",
        "AIRFLOW__CORE__AUTH_MANAGER": "airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager",
    })
    subprocess.run(["airflow", "db", "migrate"], check=True)
    # Exercise the chart's operator account creation, including a release rerun.
    for _ in range(2):
        subprocess.run(["airflow", "users", "create", "--username", "admin", "--firstname", "Test",
                        "--lastname", "Operator", "--role", "Admin", "--email", "airflow@localhost",
                        "--password", "offline-operator-password"], check=True)
    import pendulum
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder="/opt/airflow/dags")
    assert not bag.import_errors, bag.import_errors
    assert set(bag.dags) == {"ingest_study"}  # Held-back clinical DAGs are not installed.
    dag = bag.dags["ingest_study"]
    assert set(dag.task_ids) == {"select_and_admit", "wait_for_documents", "report"}
    assert dag.max_active_runs == 1 and not dag.catchup
    assert dag.timetable.serialize()["timezone"] == "Australia/Brisbane"
    assert dag.timetable.serialize()["expression"] == "0 2 * * *"
    for index, outcome in enumerate(("completed", "failed")):
        outcomes["status"] = outcome
        # Do not spend minutes retrying an intentionally failed report task.
        for task in dag.tasks:
            task.retries = 0
        kwargs = {"logical_date": None} if index else {}
        boundary = pendulum.now("Australia/Brisbane").start_of("day").subtract(days=2 - index).add(hours=2)
        result = dag.test(run_after=boundary, **kwargs)
        assert str(result.state) == ("success" if outcome == "completed" else "failed"), result.state
    assert len(selections) == 2
    print(json.dumps({"airflow": "3.3.1", "dag": dag.dag_id, "executed_runs": 2,
                      "success_and_failure_propagation": True, "workload_client": True,
                      "operator_account_rerun": True}))
    for server in (identity, api):
        server.shutdown()
