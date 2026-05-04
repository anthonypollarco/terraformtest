#!/usr/bin/env python3
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_URL = "https://cloud.tenable.com"

s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")


def get_tenable_headers() -> dict:
    secret_arn = os.environ["TENABLE_SECRET_ARN"]
    secret_value = secretsmanager.get_secret_value(SecretId=secret_arn)
    payload = json.loads(secret_value["SecretString"])
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
    except Exception:
        return now - 86400


def write_checkpoint(bucket: str, key: str, ts: int) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=str(ts).encode("utf-8"), ContentType="text/plain")


def request_export(start_time: int, headers: dict) -> str:
    payload = {
        "filters": {"indexed_at": start_time, "state": ["OPEN", "REOPENED", "FIXED"]},
        "num_assets": 500,
        "include_unlicensed": True,
    }
    response = requests.post(f"{BASE_URL}/vulns/export", headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.json().get("export_uuid")


def wait_for_chunks(export_uuid: str, headers: dict) -> list:
    url = f"{BASE_URL}/vulns/export/{export_uuid}/status"
    while True:
        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if status == "FINISHED":
            return data.get("chunks_available", [])
        if status == "ERROR":
            raise RuntimeError("Tenable backend failed to process the export.")
        time.sleep(15)


def upload_chunk(export_uuid: str, chunk_id: int, headers: dict, bucket: str, key_prefix: str):
    url = f"{BASE_URL}/vulns/export/{export_uuid}/chunks/{chunk_id}"
    key = f"{key_prefix}/raw/{export_uuid}/vulns-{chunk_id}.json"

    with requests.get(url, headers=headers, stream=True, timeout=120) as response:
        response.raise_for_status()
        s3.upload_fileobj(response.raw, bucket, key, ExtraArgs={"ContentType": "application/json"})

    return key


def lambda_handler(event, context):
    current_time = int(time.time())
    bucket = os.environ["OUTPUT_BUCKET"]
    key_prefix = os.environ.get("OUTPUT_PREFIX", "tenable-exports")
    checkpoint_key = os.environ.get("CHECKPOINT_KEY", "state/last_sync.txt")

    headers = get_tenable_headers()
    last_sync = read_checkpoint(bucket, checkpoint_key)
    export_uuid = request_export(last_sync, headers)
    chunks = wait_for_chunks(export_uuid, headers)

    uploaded_files = []
    for chunk_id in chunks:
        uploaded_files.append(upload_chunk(export_uuid, chunk_id, headers, bucket, key_prefix))

    write_checkpoint(bucket, checkpoint_key, current_time)

    run_summary = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "export_uuid": export_uuid,
        "indexed_since": last_sync,
        "chunk_count": len(chunks),
        "uploaded_files": uploaded_files,
    }

    summary_key = f"{key_prefix}/logs/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/summary-{export_uuid}.json"
    s3.put_object(Bucket=bucket, Key=summary_key, Body=json.dumps(run_summary, indent=2).encode("utf-8"), ContentType="application/json")

    logging.info("Export completed: %s", json.dumps(run_summary))
    return run_summary
