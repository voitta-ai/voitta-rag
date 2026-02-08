#!/usr/bin/env python3
"""Build BM25 sparse vectors for existing chunks in Qdrant.

Qdrant doesn't allow adding new vector fields to an existing collection,
so this script creates a NEW collection with both the original dense vectors
and a BM25 sparse vector, then migrates all data in a single pass.

The original collection is left intact as a backup.
After verifying the new collection, update QDRANT_COLLECTION in .env.

Prerequisites:
    pip install fastembed

Usage:
    python scripts/build_sparse_vectors.py
    python scripts/build_sparse_vectors.py --batch-size 1000
    python scripts/build_sparse_vectors.py --dry-run
    python scripts/build_sparse_vectors.py --target my_new_collection
"""

import argparse
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from fastembed import SparseTextEmbedding
except ImportError:
    print("ERROR: fastembed is not installed. Run: pip install fastembed")
    sys.exit(1)

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from tqdm import tqdm

from voitta.config import get_settings

SPARSE_VECTOR_NAME = "bm25"


def create_target_collection(
    client: QdrantClient,
    target: str,
    dimension: int,
) -> None:
    """Create the target collection with dense + sparse vector config."""
    print(f"  Creating collection '{target}'...")
    client.create_collection(
        collection_name=target,
        vectors_config=qmodels.VectorParams(
            size=dimension,
            distance=qmodels.Distance.COSINE,
        ),
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: qmodels.SparseVectorParams(
                modifier=qmodels.Modifier.IDF,
            ),
        },
    )
    # Recreate the same payload indexes as the original
    for field in ("file_path", "folder_path", "index_folder"):
        client.create_payload_index(
            collection_name=target,
            field_name=field,
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
    print(f"  Done")


def build_sparse_vectors(
    batch_size: int = 500,
    insert_batch_size: int = 100,
    target_name: str | None = None,
    dry_run: bool = False,
) -> None:
    settings = get_settings()
    source = settings.qdrant_collection
    target = target_name or f"{source}_v2"

    print(f"Qdrant:     {settings.qdrant_host}:{settings.qdrant_port}")
    print(f"Source:     {source}")
    print(f"Target:     {target}")

    client = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        timeout=120,
    )

    # Source collection stats
    info = client.get_collection(source)
    total_points = info.points_count or 0
    dimension = info.config.params.vectors.size
    print(f"Points:     {total_points:,}")
    print(f"Dense dim:  {dimension}")
    print()

    if total_points == 0:
        print("No points to process.")
        return

    # Create target collection
    if not dry_run:
        # Check if target already exists
        existing = [c.name for c in client.get_collections().collections]
        if target in existing:
            target_info = client.get_collection(target)
            target_count = target_info.points_count or 0
            print(f"  Target '{target}' already exists with {target_count:,} points.")
            if target_count >= total_points:
                print("  Migration appears complete. Nothing to do.")
                return
            print(f"  Resuming migration ({total_points - target_count:,} remaining)...")
            print()
        else:
            create_target_collection(client, target, dimension)
            print()

    # Load BM25 model
    print("Loading BM25 model...")
    model = SparseTextEmbedding(model_name="Qdrant/bm25")
    print("Model ready")
    print()

    # Migrate with sparse vectors
    offset = None
    processed = 0
    inserted = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    pbar = tqdm(total=total_points, unit=" chunks", desc="Migrating")

    while True:
        # Scroll source: get dense vectors + payload
        results, offset = client.scroll(
            collection_name=source,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )

        if not results:
            if offset is None:
                break
            continue

        # Separate points with text vs without
        batch_points = []
        batch_texts = []
        batch_indices = []  # indices into batch_points that have text

        for point in results:
            text = point.payload.get("text", "")
            batch_points.append(point)
            if text:
                batch_texts.append(text)
                batch_indices.append(len(batch_points) - 1)
            else:
                skipped += 1

        # Generate sparse embeddings for texts
        sparse_embeddings = []
        if batch_texts and not dry_run:
            sparse_embeddings = list(model.embed(batch_texts))

        # Build target points and insert in sub-batches
        if not dry_run:
            target_points = []
            sparse_idx = 0
            for i, point in enumerate(batch_points):
                vector_data = point.vector  # dense vector (list[float])
                vectors = {"": vector_data}  # unnamed dense

                if i in batch_indices and sparse_idx < len(sparse_embeddings):
                    emb = sparse_embeddings[sparse_idx]
                    vectors[SPARSE_VECTOR_NAME] = qmodels.SparseVector(
                        indices=emb.indices.tolist(),
                        values=emb.values.tolist(),
                    )
                    sparse_idx += 1

                target_points.append(
                    qmodels.PointStruct(
                        id=point.id,
                        vector=vectors,
                        payload=point.payload,
                    )
                )

            # Upsert in sub-batches
            for i in range(0, len(target_points), insert_batch_size):
                batch = target_points[i : i + insert_batch_size]
                try:
                    client.upsert(
                        collection_name=target,
                        points=batch,
                    )
                    inserted += len(batch)
                except Exception as e:
                    errors += len(batch)
                    tqdm.write(f"  Error inserting batch: {e}")

        processed += len(results)
        pbar.update(len(results))

        if offset is None:
            break

    pbar.close()

    # Summary
    elapsed = time.time() - start_time
    rate = processed / elapsed if elapsed > 0 else 0
    print()
    print(f"Completed in {elapsed:.1f}s ({rate:.0f} chunks/sec)")
    print(f"  Processed: {processed:,}")
    print(f"  Inserted:  {inserted:,}")
    if skipped:
        print(f"  Skipped (no text): {skipped:,}")
    if errors:
        print(f"  Errors:    {errors:,}")
    if dry_run:
        print("  (dry run — no changes made)")
    else:
        # Verify
        target_info = client.get_collection(target)
        target_count = target_info.points_count or 0
        print()
        print(f"Verification: {target_count:,} points in '{target}'")
        if target_count == total_points:
            print("  All points migrated successfully.")
        else:
            print(f"  WARNING: expected {total_points:,}, got {target_count:,}")
        print()
        print(f"To switch over, update .env:")
        print(f"  QDRANT_COLLECTION={target}")
        print()
        print(f"Original collection '{source}' is preserved as backup.")


def main():
    parser = argparse.ArgumentParser(
        description="Build BM25 sparse vectors by migrating to a new Qdrant collection"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Scroll batch size (default: 500)",
    )
    parser.add_argument(
        "--insert-batch-size",
        type=int,
        default=100,
        help="Qdrant upsert batch size (default: 100)",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target collection name (default: <source>_v2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and embed without writing",
    )
    args = parser.parse_args()

    build_sparse_vectors(
        batch_size=args.batch_size,
        insert_batch_size=args.insert_batch_size,
        target_name=args.target,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
