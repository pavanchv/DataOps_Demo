import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("WEBSITE_SITE_NAME") else "127.0.0.1")
WORKSPACE_ID = "bf2001fb-5030-4244-b7f8-2681d8bc790f"
PIPELINE_ID = "97761fbc-6c2e-4444-ae37-3a559ccd0522"
SCENARIO_B_PIPELINE_ID = "8063e804-d6de-4b8a-a750-e9357fc5dd18"
REPAIR_PIPELINE_ID = "971b2c65-c4d8-4044-90ad-a0daecc48836"
GOLD_SQL_ENDPOINT_ID = "0253e809-96c1-4e83-b502-781a4d759b84"
GOLD_SQL_DATABASE = "LK_Gold"
SILVER_SQL_ENDPOINT_ID = "43faca69-fb67-49b7-b3b1-be8910c42134"
SILVER_SQL_DATABASE = "LK_Silver"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_HTTP_TIMEOUT = int(os.environ.get("FABRIC_HTTP_TIMEOUT", "180"))
AZ = os.environ.get("AZ_CLI", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd")
ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "").strip()
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
SERVICE_PRINCIPAL_NAME = os.environ.get("FABRIC_OWNER_UPN", "").strip()
AUTH_MODE = os.environ.get("FABRIC_AUTH_MODE", "auto").strip().lower()
_TOKEN_CACHE = {}


SQL_QUERIES = {
    "gold_website_orders": """
SET NOCOUNT ON;
SELECT TOP 10
  Order_ID,
  Source_App,
  Order_Datetime,
  Customer_Name,
  Product_Name,
  Quantity,
  Net_Amount,
  Order_Status,
  Ingestion_Timestamp
FROM dbo.gold_website_orders
ORDER BY Ingestion_Timestamp DESC;
""",
    "gold_ecomm_orders": """
SET NOCOUNT ON;
SELECT TOP 10
  Order_ID,
  Transaction_Date,
  Product,
  Quantity,
  Price,
  Total_Amount,
  Source_File,
  Ingestion_Timestamp
FROM dbo.gold_ecomm_orders
ORDER BY Ingestion_Timestamp DESC;
""",
}


def run_az(args):
    completed = subprocess.run(
        [AZ, *args],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return completed.stdout.strip()


def service_principal_configured():
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET)


def use_service_principal():
    if AUTH_MODE == "service_principal":
        return True
    if AUTH_MODE == "azure_cli":
        return False
    return service_principal_configured()


def get_service_principal_token(scope):
    import time

    cache_key = ("sp", scope)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached["expires_at"] > time.time() + 120:
        return cached["access_token"]

    if not service_principal_configured():
        raise RuntimeError(
            "Service principal auth selected, but AZURE_TENANT_ID, AZURE_CLIENT_ID, or AZURE_CLIENT_SECRET is missing."
        )

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    form = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": scope,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=form,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8")
        raise RuntimeError(f"Microsoft Entra token request failed: {detail}")

    access_token = body["access_token"]
    expires_in = int(body.get("expires_in", 3599))
    _TOKEN_CACHE[cache_key] = {"access_token": access_token, "expires_at": time.time() + expires_in}
    return access_token


def get_fabric_token():
    if use_service_principal():
        return get_service_principal_token("https://api.fabric.microsoft.com/.default")
    raw = run_az(["account", "get-access-token", "--resource", "https://api.fabric.microsoft.com", "--output", "json"])
    return json.loads(raw)["accessToken"]


def get_database_token():
    if use_service_principal():
        return get_service_principal_token("https://database.windows.net/.default")
    raw = run_az(["account", "get-access-token", "--resource", "https://database.windows.net/", "--output", "json"])
    return json.loads(raw)["accessToken"]


def get_user_context():
    if use_service_principal():
        return {
            "userPrincipalName": SERVICE_PRINCIPAL_NAME,
            "ownerObjectId": "",
        }
    account = json.loads(run_az(["account", "show", "--output", "json"]))
    owner_object_id = ""
    try:
        owner_object_id = run_az(["ad", "signed-in-user", "show", "--query", "id", "--output", "tsv"])
    except Exception:
        pass
    return {
        "userPrincipalName": account.get("user", {}).get("name", ""),
        "ownerObjectId": owner_object_id,
    }


def fabric_request(url, method="GET", payload=None):
    token = get_fabric_token()
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=FABRIC_HTTP_TIMEOUT) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return response.status, dict(response.headers), parsed
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        detail_parts = []
        for key in ("requestId", "errorCode", "message", "moreDetails", "relatedResource"):
            value = parsed.get(key)
            if value:
                detail_parts.append(f"{key}: {value}")
        if not detail_parts:
            detail_parts.append(json.dumps(parsed))
        raise RuntimeError(f"Fabric API {error.code}: " + " | ".join(detail_parts))
    except (TimeoutError, socket.timeout) as error:
        raise RuntimeError(
            f"Fabric API request timed out after {FABRIC_HTTP_TIMEOUT} seconds. "
            "Fabric may still have accepted the request; check the pipeline monitor before starting it again."
        ) from error


def get_sql_server(endpoint_id):
    url = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/sqlEndpoints/{endpoint_id}/connectionString"
    _, _, body = fabric_request(url)
    connection_string = body.get("connectionString") if body else ""
    if not connection_string:
        raise RuntimeError("Fabric did not return a SQL endpoint connection string.")
    return connection_string


def normalize_sql_value(value):
    try:
        from datetime import date, datetime
        from decimal import Decimal

        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass

    if isinstance(value, str):
        match = re.fullmatch(r"/Date\((-?\d+)\)/", value)
        if match:
            from datetime import datetime, timezone

            millis = int(match.group(1))
            return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
    return value


def run_sql_query(database, endpoint_id, query, params=None):
    try:
        import pyodbc
    except ImportError as error:
        raise RuntimeError(
            "pyodbc is required for hosted SQL access. Install requirements.txt before running hosted mode."
        ) from error

    server = get_sql_server(endpoint_id)
    token = get_database_token()
    connection_string = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    token_bytes = token.encode("utf-16-le")
    token_struct = len(token_bytes).to_bytes(4, "little") + token_bytes

    with pyodbc.connect(connection_string, attrs_before={1256: token_struct}) as connection:
        cursor = connection.cursor()
        cursor.execute(query, params or [])
        columns = [column[0] for column in cursor.description]
        rows = []
        for row in cursor.fetchall():
            rows.append(
                {
                    columns[index]: normalize_sql_value(row[index])
                    for index in range(len(columns))
                }
            )
        return rows


def get_gold_website_orders():
    return run_sql_query(GOLD_SQL_DATABASE, GOLD_SQL_ENDPOINT_ID, SQL_QUERIES["gold_website_orders"])


def get_gold_ecomm_orders(since="", limit=10):
    where_clause = ""
    params = []
    if since:
        where_clause = "WHERE Ingestion_Timestamp >= ?"
        params.append(since)
    query = f"""
SET NOCOUNT ON;
SELECT TOP {safe_int(limit, 10)}
  Order_ID,
  Transaction_Date,
  Product,
  Quantity,
  Price,
  Total_Amount,
  Source_File,
  Ingestion_Timestamp
FROM dbo.gold_ecomm_orders
{where_clause}
ORDER BY Ingestion_Timestamp DESC;
"""
    return run_sql_query(GOLD_SQL_DATABASE, GOLD_SQL_ENDPOINT_ID, query, params)


def get_quality_results(table_filter):
    where = ""
    params = []
    if table_filter:
        where = "WHERE Table_Name LIKE ?"
        params.append(f"%{table_filter}%")
    query = f"""
SET NOCOUNT ON;
SELECT TOP 1
  Total_Rows,
  Clean_Rows,
  Bad_Rows,
  Duplicate_Rows,
  Quality_Score_Percentage,
  Table_Name,
  Runtime_Seconds,
  Runtime_Minutes,
  Check_Timestamp
FROM dbo.data_quality_results
{where}
ORDER BY Check_Timestamp DESC;
"""
    return run_sql_query(
        SILVER_SQL_DATABASE,
        SILVER_SQL_ENDPOINT_ID,
        query,
        params,
    )


def safe_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def get_quarantine_ecomm_orders(since="", limit=10, active_only=True, run_id=""):
    where_parts = []
    params = []
    if run_id == "latest":
        latest_where = []
        latest_params = []
        if since:
            latest_where.append("Quarantine_Timestamp >= ?")
            latest_params.append(since)
        if active_only:
            latest_where.append("(Is_Repaired IS NULL OR Is_Repaired = 0)")
        latest_clause = ("WHERE " + " AND ".join(latest_where)) if latest_where else ""
        latest_query = f"""
SET NOCOUNT ON;
SELECT TOP 1 Run_ID
FROM dbo.quarantine_ecomm_orders
{latest_clause}
ORDER BY Quarantine_Timestamp DESC;
"""
        latest_rows = run_sql_query(SILVER_SQL_DATABASE, SILVER_SQL_ENDPOINT_ID, latest_query, latest_params)
        run_id = str(latest_rows[0]["Run_ID"]) if latest_rows else ""

    if since:
        where_parts.append("Quarantine_Timestamp >= ?")
        params.append(since)
    if run_id:
        where_parts.append("Run_ID = ?")
        params.append(run_id)
    if active_only:
        where_parts.append("(Is_Repaired IS NULL OR Is_Repaired = 0)")

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    query = f"""
SET NOCOUNT ON;
SELECT TOP {safe_int(limit, 10)}
  Order_ID,
  Price,
  Error_Reason,
  Quarantine_Timestamp,
  Run_ID,
  Is_Repaired,
  Repaired_Timestamp
FROM dbo.quarantine_ecomm_orders
{where_clause}
ORDER BY Quarantine_Timestamp DESC;
"""
    return run_sql_query(SILVER_SQL_DATABASE, SILVER_SQL_ENDPOINT_ID, query, params)


def write_json(handler, status, body):
    encoded = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def do_POST(self):
        if self.path not in ("/api/run-scenario-a", "/api/run-scenario-b", "/api/run-repair"):
            write_json(self, 404, {"ok": False, "error": "Unknown API endpoint"})
            return

        try:
            pipeline_id = {
                "/api/run-scenario-a": PIPELINE_ID,
                "/api/run-scenario-b": SCENARIO_B_PIPELINE_ID,
                "/api/run-repair": REPAIR_PIPELINE_ID,
            }[self.path]
            user = get_user_context()
            execution_data = {
                "pipelineName": "pipeline",
            }
            if user["userPrincipalName"]:
                execution_data["OwnerUserPrincipalName"] = user["userPrincipalName"]
            if user["ownerObjectId"]:
                execution_data["OwnerUserObjectId"] = user["ownerObjectId"]

            url = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{pipeline_id}/jobs/instances?jobType=Pipeline"
            status, headers, body = fabric_request(url, method="POST", payload={"executionData": execution_data})
            location = headers.get("Location", "")
            job_instance_id = location.rstrip("/").split("/")[-1] if location else ""
            write_json(
                self,
                202,
                {
                    "ok": True,
                    "workspaceId": WORKSPACE_ID,
                    "pipelineId": pipeline_id,
                    "jobInstanceId": job_instance_id,
                    "location": location,
                    "retryAfter": int(headers.get("Retry-After", "5")),
                    "fabricStatus": status,
                    "body": body,
                },
            )
        except Exception as error:
            write_json(self, 500, {"ok": False, "error": str(error)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            write_json(
                self,
                200,
                {
                    "ok": True,
                    "authMode": "service_principal" if use_service_principal() else "azure_cli",
                    "servicePrincipalConfigured": service_principal_configured(),
                    "workspaceId": WORKSPACE_ID,
                    "host": HOST,
                    "port": PORT,
                },
            )
            return

        if parsed.path == "/api/gold-website-orders":
            try:
                rows = get_gold_website_orders()
                write_json(self, 200, {"ok": True, "rows": rows})
            except Exception as error:
                write_json(self, 500, {"ok": False, "error": str(error)})
            return

        if parsed.path == "/api/quarantine-ecomm-orders":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                since = params.get("since", [""])[0]
                limit = safe_int(params.get("limit", ["10"])[0], 10)
                active_only = params.get("activeOnly", ["true"])[0].lower() != "false"
                run_id = params.get("runId", [""])[0]
                rows = get_quarantine_ecomm_orders(since=since, limit=limit, active_only=active_only, run_id=run_id)
                write_json(self, 200, {"ok": True, "rows": rows})
            except Exception as error:
                write_json(self, 500, {"ok": False, "error": str(error)})
            return

        if parsed.path == "/api/gold-ecomm-orders":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                since = params.get("since", [""])[0]
                limit = safe_int(params.get("limit", ["10"])[0], 10)
                rows = get_gold_ecomm_orders(since=since, limit=limit)
                write_json(self, 200, {"ok": True, "rows": rows})
            except Exception as error:
                write_json(self, 500, {"ok": False, "error": str(error)})
            return

        if parsed.path == "/api/quality-results":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                table_filter = params.get("tableFilter", [""])[0]
                rows = get_quality_results(table_filter)
                write_json(self, 200, {"ok": True, "rows": rows})
            except Exception as error:
                write_json(self, 500, {"ok": False, "error": str(error)})
            return

        if parsed.path == "/api/pipeline-status":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                job_instance_id = params.get("jobInstanceId", [""])[0]
                pipeline_id = params.get("pipelineId", [PIPELINE_ID])[0]
                if not job_instance_id:
                    write_json(self, 400, {"ok": False, "error": "Missing jobInstanceId"})
                    return

                url = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{pipeline_id}/jobs/instances/{job_instance_id}"
                _, _, body = fabric_request(url)
                write_json(self, 200, {"ok": True, "job": body})
            except Exception as error:
                write_json(self, 500, {"ok": False, "error": str(error)})
            return

        return super().do_GET()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), DemoHandler)
    print(f"DataOps demo running at http://{HOST}:{PORT}")
    print(f"Authentication mode: {'service_principal' if use_service_principal() else 'azure_cli'}")
    server.serve_forever()
