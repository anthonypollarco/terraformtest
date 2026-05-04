#!/usr/bin/env python3
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_URL = "https://cloud.tenable.com"
TERMINAL_STATES = {"FINISHED", "ERROR"}

s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")


def _build_session() -> requests.Session:
    max_attempts = int(os.environ.get("HTTP_MAX_ATTEMPTS", "8"))
    backoff = float(os.environ.get("HTTP_BACKOFF_SECONDS", "2"))

    retry = Retry(
        total=max_attempts,
        connect=max_attempts,
        read=max_attempts,
        status=max_attempts,
        backoff_factor=backoff,
        status_forcelist=[408, 425, 429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_tenable_headers() -> dict:
    secret_arn = os.environ["TENABLE_SECRET_ARN"]
    secret_value = secretsmanager.get_secret_value(SecretId=secret_arn)
    payload = json.loads(secret_value["SecretString"])

    if "accessKey" not in payload or "secretKey" not in payload:
        raise ValueError("Secret is missing required keys: accessKey and secretKey")

    return {
        "X-ApiKeys": f"accessKey={payload['accessKey']}; secretKey={payload['secretKey']}",
        "accept": "application/json",
    }


def read_checkpoint(bucket: str, key: str) -> int:
    now = int(time.time())
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        return int(response["Body"].read().decode("utf-8").strip())
    except s3.exceptions.NoSuchKey:
        return now - 86400
    except Exception as exc:
        logging.warning("Checkpoint read failed; defaulting to last 24h: %s", exc)
        return now - 86400


def write_checkpoint(bucket: str, key: str, ts: int) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=str(ts).encode("utf-8"), ContentType="text/plain")


def request_export(start_time: int, headers: dict, session: requests.Session) -> str:
    payload = {
        "filters": {"indexed_at": start_time, "state": ["OPEN", "REOPENED", "FIXED"]},
        "num_assets": 500,
        "include_unlicensed": True,
    }
    response = session.post(f"{BASE_URL}/vulns/export", headers=headers, json=payload, timeout=60)
    response.raise_for_status()

    export_uuid = response.json().get("export_uuid")
    if not export_uuid:
        raise RuntimeError("Tenable did not return export_uuid")
    return export_uuid


def wait_for_chunks(export_uuid: str, headers: dict, session: requests.Session) -> list:
    url = f"{BASE_URL}/vulns/export/{export_uuid}/status"
    max_polls = int(os.environ.get("STATUS_MAX_POLLS", "240"))
    poll_interval = int(os.environ.get("STATUS_POLL_SECONDS", "15"))

    for _ in range(max_polls):
        response = session.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        status = data.get("status")

        if status == "FINISHED":
            return data.get("chunks_available", [])
        if status == "ERROR":
            raise RuntimeError("Tenable export status is ERROR")
        if status not in TERMINAL_STATES:
            time.sleep(poll_interval)

    raise TimeoutError(f"Export {export_uuid} did not finish within polling window")


def upload_chunk(export_uuid: str, chunk_id: int, headers: dict, session: requests.Session, bucket: str, key_prefix: str):
    url = f"{BASE_URL}/vulns/export/{export_uuid}/chunks/{chunk_id}"
    key = f"{key_prefix}/raw/{export_uuid}/vulns-{chunk_id}.json"

    with session.get(url, headers=headers, stream=True, timeout=120) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        s3.upload_fileobj(response.raw, bucket, key, ExtraArgs={"ContentType": "application/json"})

    return key


def lambda_handler(event, context):
    current_time = int(time.time())
    bucket = os.environ["OUTPUT_BUCKET"]
    key_prefix = os.environ.get("OUTPUT_PREFIX", "tenable-exports")
    checkpoint_key = os.environ.get("CHECKPOINT_KEY", "state/last_sync.txt")

    session = _build_session()
    headers = get_tenable_headers()
    last_sync = read_checkpoint(bucket, checkpoint_key)

    export_uuid = request_export(last_sync, headers, session)
    chunks = wait_for_chunks(export_uuid, headers, session)

    uploaded_files = []
    for chunk_id in chunks:
        uploaded_files.append(upload_chunk(export_uuid, chunk_id, headers, session, bucket, key_prefix))

    write_checkpoint(bucket, checkpoint_key, current_time)

    cst = timezone(timedelta(hours=-6), name="CST")
    run_time_cst = datetime.now(cst)
    run_summary = {
        "run_timestamp_cst": run_time_cst.isoformat(),
        "export_uuid": export_uuid,
        "indexed_since": last_sync,
        "chunk_count": len(chunks),
        "uploaded_files": uploaded_files,
    }

    summary_key = f"{key_prefix}/logs/{run_time_cst.strftime('%Y/%m/%d')}/summary-{export_uuid}.json"
    s3.put_object(Bucket=bucket, Key=summary_key, Body=json.dumps(run_summary, indent=2).encode("utf-8"), ContentType="application/json")

    logging.info("Export completed: %s", json.dumps(run_summary))
    return run_summary
