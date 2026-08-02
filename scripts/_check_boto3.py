"""Check what S3/awslib tools we have."""
import sys
try:
    import boto3
    print(f"boto3: {boto3.__version__}")
except ImportError:
    print("boto3: NOT INSTALLED")

try:
    import lz4
    print(f"lz4: {lz4.__version__ if hasattr(lz4, '__version__') else 'available'}")
except ImportError:
    print("lz4: NOT INSTALLED")

try:
    import smart_open
    print(f"smart_open: {smart_open.__version__}")
except ImportError:
    print("smart_open: NOT INSTALLED")

try:
    import s3fs
    print(f"s3fs: {s3fs.__version__}")
except ImportError:
    print("s3fs: NOT INSTALLED")
