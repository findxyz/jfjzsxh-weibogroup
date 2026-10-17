#!/usr/bin/env python3
"""把本地文件上传到腾讯云 COS，用于 ima 知识库入库。

读取 create_media（ima MCP）返回的凭证 JSON，用 cos-python-sdk-v5 PUT 上传。

用法:
    python cos_upload.py --creds <creds.json> --file <local_file> --content-type text/csv

creds.json 可以是 create_media 的完整返回（含 media_id 与 cos_credential），
也可以只是 cos_credential 子对象。所需字段：token, secret_id, secret_key,
bucket_name, region, cos_key。

依赖：cos-python-sdk-v5（项目已 uv add）。运行：uv run python cos_upload.py ...
成功打印 "OK_RESULT" 与 ETag；失败非零退出。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path


def parse_creds(creds_obj: dict) -> dict:
    """兼容完整 create_media 返回或纯 cos_credential 子对象。"""
    if "cos_credential" in creds_obj:
        creds_obj = creds_obj["cos_credential"]
    required = ("token", "secret_id", "secret_key", "bucket_name", "region", "cos_key")
    missing = [k for k in required if not creds_obj.get(k)]
    if missing:
        raise ValueError(f"凭证缺少字段: {missing}")
    return {k: creds_obj[k] for k in required}


def main() -> int:
    ap = argparse.ArgumentParser(description="上传文件到 COS（ima 知识库入库用）")
    ap.add_argument("--creds", required=True, help="create_media 返回的凭证 JSON 路径")
    ap.add_argument("--file", required=True, help="待上传的本地文件路径")
    ap.add_argument("--content-type", default="text/csv", help="MIME 类型（csv→text/csv）")
    args = ap.parse_args()

    creds = parse_creds(json.loads(Path(args.creds).read_text(encoding="utf-8")))
    file_path = args.file
    if not os.path.isfile(file_path):
        print(f"文件不存在: {file_path}", file=sys.stderr)
        return 1
    size = os.path.getsize(file_path)
    print(f"上传文件: {file_path} ({size} bytes)")

    from qcloud_cos import CosConfig, CosS3Client
    from qcloud_cos.cos_exception import CosServiceError

    config = CosConfig(
        Region=creds["region"],
        SecretId=creds["secret_id"],
        SecretKey=creds["secret_key"],
        Token=creds["token"],
        Scheme="https",
    )
    client = CosS3Client(config)

    with open(file_path, "rb") as f:
        data = f.read()

    try:
        resp = client.put_object(
            Bucket=creds["bucket_name"],
            Body=data,
            Key=creds["cos_key"],
            ContentType=args.content_type,
        )
    except CosServiceError as e:
        print(f"上传失败 CosServiceError: status={e.get_status_code()} "
              f"code={e.get_error_code()} msg={e.get_error_msg()}", file=sys.stderr)
        return 1

    print("上传成功")
    print("  ETag:", resp.get("ETag"))
    print("OK_RESULT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
