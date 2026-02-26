"""AWS Glue Data Catalog sync connector - indexes schema metadata (databases, tables, columns)."""

import hashlib
import logging
from pathlib import Path

import boto3

from .base import BaseSyncConnector, RemoteFile

logger = logging.getLogger(__name__)


def _get_glue_client(source):
    """Build a boto3 Glue client from the sync source config."""
    kwargs = {}
    if source.glue_region:
        kwargs["region_name"] = source.glue_region

    if source.glue_access_key_id and source.glue_secret_access_key:
        session = boto3.Session(
            aws_access_key_id=source.glue_access_key_id,
            aws_secret_access_key=source.glue_secret_access_key,
            **kwargs,
        )
    elif source.glue_profile:
        session = boto3.Session(profile_name=source.glue_profile, **kwargs)
    else:
        session = boto3.Session(**kwargs)

    retval = session.client("glue")
    return retval


def _get_databases(client, catalog_id: str | None, db_filter: str | None) -> list[dict]:
    """Fetch databases from Glue, optionally filtered."""
    kwargs = {}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id

    databases = []
    paginator = client.get_paginator("get_databases")
    for page in paginator.paginate(**kwargs):
        for db in page.get("DatabaseList", []):
            databases.append(db)

    if db_filter and db_filter != "*":
        allowed = {name.strip().lower() for name in db_filter.split(",") if name.strip()}
        databases = [db for db in databases if db["Name"].lower() in allowed]

    return databases


def _get_tables(client, database_name: str, catalog_id: str | None) -> list[dict]:
    """Fetch all tables for a database."""
    kwargs = {"DatabaseName": database_name}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id

    tables = []
    paginator = client.get_paginator("get_tables")
    for page in paginator.paginate(**kwargs):
        for table in page.get("TableList", []):
            tables.append(table)

    return tables


def _render_database_md(db: dict, tables: list[dict]) -> str:
    """Render a database summary as markdown."""
    name = db["Name"]
    description = db.get("Description", "")
    location = db.get("LocationUri", "")
    params = db.get("Parameters", {})

    lines = [f"# Database: {name}\n"]

    if description:
        lines.append(f"{description}\n")

    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Name | {name} |")
    if location:
        lines.append(f"| Location | {location} |")
    if params:
        for k, v in params.items():
            lines.append(f"| {k} | {v} |")
    lines.append(f"| Table Count | {len(tables)} |")
    lines.append("")

    if tables:
        lines.append("## Tables\n")
        lines.append("| Table | Type | Columns | Partition Keys |")
        lines.append("|---|---|---|---|")
        for t in sorted(tables, key=lambda t: t["Name"]):
            tname = t["Name"]
            ttype = t.get("TableType", "")
            cols = t.get("StorageDescriptor", {}).get("Columns", [])
            pkeys = t.get("PartitionKeys", [])
            lines.append(f"| {tname} | {ttype} | {len(cols)} | {len(pkeys)} |")
        lines.append("")

    retval = "\n".join(lines)
    return retval


def _render_table_md(table: dict, database_name: str) -> str:
    """Render a single table's metadata as markdown."""
    name = table["Name"]
    description = table.get("Description", "")
    table_type = table.get("TableType", "")
    create_time = table.get("CreateTime", "")
    update_time = table.get("UpdateTime", "")
    owner = table.get("Owner", "")
    params = table.get("Parameters", {})
    sd = table.get("StorageDescriptor", {})
    columns = sd.get("Columns", [])
    location = sd.get("Location", "")
    input_format = sd.get("InputFormat", "")
    output_format = sd.get("OutputFormat", "")
    serde_info = sd.get("SerdeInfo", {})
    serde_lib = serde_info.get("SerializationLibrary", "")
    serde_params = serde_info.get("Parameters", {})
    partition_keys = table.get("PartitionKeys", [])

    lines = [f"# Table: {database_name}.{name}\n"]

    if description:
        lines.append(f"{description}\n")

    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Database | {database_name} |")
    lines.append(f"| Table | {name} |")
    if table_type:
        lines.append(f"| Type | {table_type} |")
    if owner:
        lines.append(f"| Owner | {owner} |")
    if location:
        lines.append(f"| Location | {location} |")
    if input_format:
        lines.append(f"| Input Format | {input_format} |")
    if output_format:
        lines.append(f"| Output Format | {output_format} |")
    if serde_lib:
        lines.append(f"| SerDe | {serde_lib} |")
    if create_time:
        lines.append(f"| Created | {create_time} |")
    if update_time:
        lines.append(f"| Updated | {update_time} |")
    lines.append("")

    # Columns
    if columns:
        lines.append("## Columns\n")
        lines.append("| # | Name | Type | Comment |")
        lines.append("|---|---|---|---|")
        for i, col in enumerate(columns, 1):
            cname = col.get("Name", "")
            ctype = col.get("Type", "")
            ccomment = col.get("Comment", "")
            lines.append(f"| {i} | {cname} | {ctype} | {ccomment} |")
        lines.append("")

    # Partition keys
    if partition_keys:
        lines.append("## Partition Keys\n")
        lines.append("| # | Name | Type | Comment |")
        lines.append("|---|---|---|---|")
        for i, pk in enumerate(partition_keys, 1):
            pname = pk.get("Name", "")
            ptype = pk.get("Type", "")
            pcomment = pk.get("Comment", "")
            lines.append(f"| {i} | {pname} | {ptype} | {pcomment} |")
        lines.append("")

    # SerDe parameters
    if serde_params:
        lines.append("## SerDe Parameters\n")
        lines.append("| Key | Value |")
        lines.append("|---|---|")
        for k, v in sorted(serde_params.items()):
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # Table parameters
    if params:
        lines.append("## Table Parameters\n")
        lines.append("| Key | Value |")
        lines.append("|---|---|")
        for k, v in sorted(params.items()):
            lines.append(f"| {k} | {v} |")
        lines.append("")

    retval = "\n".join(lines)
    return retval


class GlueCatalogConnector(BaseSyncConnector):

    async def list_files(self, source) -> list[RemoteFile]:
        if not source.glue_region:
            raise RuntimeError("AWS region not configured")

        client = _get_glue_client(source)
        catalog_id = source.glue_catalog_id or None
        db_filter = source.glue_databases or None

        databases = _get_databases(client, catalog_id, db_filter)
        files: list[RemoteFile] = []

        for db in databases:
            db_name = db["Name"]
            tables = _get_tables(client, db_name, catalog_id)

            # Database summary file
            db_hash = hashlib.sha256(
                str(len(tables)).encode() + db_name.encode()
            ).hexdigest()
            files.append(RemoteFile(
                remote_path=f"databases/{db_name}/_database.md",
                size=0,
                modified_at="",
                content_hash=db_hash,
            ))

            # Per-table files
            for table in tables:
                table_name = table["Name"]
                update_time = str(table.get("UpdateTime", ""))
                content_hash = hashlib.sha256(update_time.encode()).hexdigest()
                files.append(RemoteFile(
                    remote_path=f"databases/{db_name}/{table_name}.md",
                    size=0,
                    modified_at=update_time,
                    content_hash=content_hash,
                ))

        logger.info("Listed %d files from Glue catalog (%d databases)", len(files), len(databases))
        return files

    async def download_file(self, source, remote_path: str, local_path: Path) -> None:
        client = _get_glue_client(source)
        catalog_id = source.glue_catalog_id or None

        parts = remote_path.split("/")
        # Expected: databases/{db_name}/_database.md or databases/{db_name}/{table}.md
        if len(parts) < 3 or parts[0] != "databases":
            raise RuntimeError(f"Cannot parse path: {remote_path}")

        db_name = parts[1]
        filename = parts[2]

        if filename == "_database.md":
            # Database summary
            kwargs = {"Name": db_name}
            if catalog_id:
                kwargs["CatalogId"] = catalog_id
            resp = client.get_database(**kwargs)
            db = resp["Database"]
            tables = _get_tables(client, db_name, catalog_id)
            content = _render_database_md(db, tables)
        else:
            # Table file
            table_name = filename.replace(".md", "")
            kwargs = {"DatabaseName": db_name, "Name": table_name}
            if catalog_id:
                kwargs["CatalogId"] = catalog_id
            resp = client.get_table(**kwargs)
            table = resp["Table"]
            content = _render_table_md(table, db_name)

        local_path.write_text(content, encoding="utf-8")
