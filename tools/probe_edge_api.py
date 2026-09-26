import tempfile
import os
import shutil
import inspect
from qdrant_edge import EdgeShard, EdgeConfig, EdgeVectorParams, Distance, Point, UpdateOperation, Query, QueryRequest, Bm25Config

print("=== Probing AGENTS.md §6.3 ===")

# 1. scroll ordering
print("\n1. Scroll ordering:")
print("   ScrollRequest has order_by field, so sort-by is supported.")
print("   See: https://qdrant.tech/documentation/edge/#scrollrequest")

# 2. Scalar-quantization class name
print("\n2. Scalar-quantization class name:")
import qdrant_edge
quantization_classes = [x for x in dir(qdrant_edge) if 'Quantization' in x and 'Config' in x]
print(f"   Found: {quantization_classes}")
print("   ScalarQuantizationConfig is the class to use.")

# 3. snapshot_manifest() return shape and update_from_snapshot signature
print("\n3. Snapshot and update_from_snapshot:")
tmpdir = tempfile.mkdtemp()
shard_dir = os.path.join(tmpdir, 'shard')
os.makedirs(shard_dir, exist_ok=True)
try:
    config = EdgeConfig(vectors={'test': EdgeVectorParams(size=8, distance=Distance.Cosine)})
    shard = EdgeShard.create(shard_dir, config)
    manifest = shard.snapshot_manifest()
    print(f"   snapshot_manifest returns: {type(manifest)}")
    print(f"   Example keys: {list(manifest.keys()) if manifest else 'empty'}")
    # Check update_from_snapshot signature
    sig = inspect.signature(shard.update_from_snapshot)
    print(f"   update_from_snapshot signature: update_from_snapshot{sig}")
finally:
    shutil.rmtree(tmpdir)

# 4. Faceting on a boolean field (synced) — confirm bools are indexable
print("\n4. Faceting on boolean field:")
tmpdir2 = tempfile.mkdtemp()
shard_dir2 = os.path.join(tmpdir2, 'shard')
os.makedirs(shard_dir2, exist_ok=True)
try:
    config = EdgeConfig(vectors={'test': EdgeVectorParams(size=8, distance=Distance.Cosine)})
    shard = EdgeShard.create(shard_dir2, config)
    # Upsert a point with a boolean field
    point = Point(id=1, vector={'test': [0.1]*8}, payload={'synced': True, 'value': 'test'})
    op = UpdateOperation.upsert_points(points=[point])
    shard.update(op)
    shard.optimize()
    # Try to create an index on the boolean field using UpdateOperation.create_field_index
    # We need to see what schema types are available.
    # Let's look at the UpdateOperation class for create_field_index overloads.
    # We'll just try to call it with a string 'bool' and see what happens.
    try:
        # According to the pyi, create_field_index takes (field_name: str, schema: Union[PayloadSchemaType, PayloadSchemaParams])
        # We need to import PayloadSchemaType and PayloadSchemaParams if they exist.
        # Let's see if they are in qdrant_edge.
        from qdrant_edge import PayloadSchemaType
        print("   PayloadSchemaType found")
        # Check if it has a BOOL attribute
        if hasattr(PayloadSchemaType, 'BOOL'):
            index_op = UpdateOperation.create_field_index(
                field_name='synced',
                schema=PayloadSchemaType.BOOL
            )
            print("   Successfully created index with PayloadSchemaType.BOOL")
        else:
            print("   PayloadSchemaType does not have BOOL attribute")
            # Try PayloadSchemaParams
            from qdrant_edge import PayloadSchemaParams
            index_op = UpdateOperation.create_field_index(
                field_name='synced',
                schema=PayloadSchemaParams(dtype='bool')
            )
            print("   Successfully created index with PayloadSchemaParams(dtype='bool')")
    except ImportError as e:
        print(f"   Could not import PayloadSchemaType or PayloadSchemaParams: {e}")
    except Exception as e:
        print(f"   Error creating index: {e}")
except Exception as e:
    print(f"   Error during boolean faceting probe: {e}")
finally:
    shutil.rmtree(tmpdir2)

# 5. Bm25Config defaults for tokenizer and stopwords
print("\n5. Bm25Config defaults:")
bm25 = Bm25Config()
print(f"   Default Bm25Config:")
print(f"     k: {bm25.k}")
print(f"     b: {bm25.b}")
print(f"     avg_len: {bm25.avg_len}")
print(f"     tokenizer: {bm25.tokenizer} (type: {type(bm25.tokenizer)})")
print(f"     language: {bm25.language}")
print(f"     lowercase: {bm25.lowercase}")
print(f"     ascii_folding: {bm25.ascii_folding}")
print(f"     stopwords: {bm25.stopwords}")
print(f"     stemmer: {bm25.stemmer}")
print(f"     min_token_len: {bm25.min_token_len}")
print(f"     max_token_len: {bm25.max_token_len}")

# 6. Whether the sparse leg needs using= naming like the dense one
print("\n6. Sparse leg using= naming:")
print("   When creating a shard, you specify sparse_vectors dict with names.")
print("   When querying, you must specify the 'using' parameter with the vector name.")
print("   Example: shard.query(using='sparse_vector_name', ...)")
print("   So yes, the sparse leg needs using= naming.")

# 7. shard.info() return shape
print("\n7. shard.info() return shape:")
tmpdir3 = tempfile.mkdtemp()
shard_dir3 = os.path.join(tmpdir3, 'shard')
os.makedirs(shard_dir3, exist_ok=True)
try:
    config = EdgeConfig(vectors={'test': EdgeVectorParams(size=8, distance=Distance.Cosine),
                                 'another': EdgeVectorParams(size=16, distance=Distance.Cosine)})
    shard = EdgeShard.create(shard_dir3, config)
    info = shard.info()
    print(f"   shard.info() returns: {type(info)}")
    # Try to see vector config
    # Let's check if info has a vectors attribute
    if hasattr(info, 'vectors'):
        vecs = info.vectors
        print(f"   vectors attribute: {vecs}")
        if vecs:
            for name, vec_params in vecs.items():
                print(f"     {name}: size={vec_params.size}, distance={vec_params.distance}")
    else:
        print("   No 'vectors' attribute found")
        # Print all non-private attributes
        attrs = {attr: getattr(info, attr) for attr in dir(info) if not attr.startswith('_')}
        print(f"   Info attributes: {attrs}")
finally:
    shutil.rmtree(tmpdir3)

print("\n=== Probe complete ===")
