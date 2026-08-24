# Load .env (if present) before any submodule reads os.environ at import time.
# Optional dependency: absent python-dotenv simply means export-based config.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass
