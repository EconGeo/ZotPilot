"""Index all Zotero libraries into a single unified ChromaDB collection.

Usage:
    python scripts/index_all_libraries.py

Rules:
    - Stop the ZotPilot MCP server before running (pkill -f "zotpilot mcp serve")
    - Restart MCP after completion (/mcp in Claude Code)
    - Already-indexed papers are skipped; safe to re-run after adding new papers

Libraries indexed (edit LIBRARIES to add/remove):
    1  personal
    3  affordable_housing
    4  regenerative_paradigm
    7  ESG_Collaboration
    8  NAR_settlement
"""
import logging
import sys
from zotpilot.config import Config
from zotpilot.indexer import Indexer
from zotpilot.zotero_client import ZoteroClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/tmp/zotpilot-index-all.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

LIBRARIES = [
    (1, "personal"),
    (3, "affordable_housing"),
    (4, "regenerative_paradigm"),
    (7, "ESG_Collaboration"),
    (8, "NAR_settlement"),
]

config = Config.load()

# Pre-flight: collect all doc IDs so reconciliation never deletes cross-library docs
logger.info("=== Pre-flight: collecting doc IDs from all libraries ===")
all_doc_ids: dict[int, set[str]] = {}
for library_id, name in LIBRARIES:
    try:
        zc = ZoteroClient(config.zotero_data_dir, library_id=library_id)
        ids = {i.item_key for i in zc.get_all_items_with_pdfs()
               if i.pdf_path and i.pdf_path.exists()}
        all_doc_ids[library_id] = ids
        logger.info(f"  {name}: {len(ids)} PDFs found")
    except Exception as e:
        logger.warning(f"  {name}: could not pre-collect doc IDs ({e})")
        all_doc_ids[library_id] = set()

universe = set().union(*all_doc_ids.values())
logger.info(f"=== Total unique PDFs across all libraries: {len(universe)} ===")

# Index each library, protecting all other libraries' doc IDs from reconciliation
for library_id, name in LIBRARIES:
    protected = universe - all_doc_ids.get(library_id, set())
    logger.info(f"=== Starting library: {name} (library_id={library_id}, protected={len(protected)}) ===")
    try:
        indexer = Indexer(config, library_id=library_id)
        stats = indexer.index_all(batch_size=None, protected_doc_ids=protected)
        logger.info(
            f"=== Done {name}: indexed={stats.get('indexed', 0)} "
            f"skipped={stats.get('already_indexed', 0)} "
            f"failed={stats.get('failed', 0)} ==="
        )
    except Exception as e:
        logger.error(f"=== FAILED {name}: {e} ===")

logger.info("All libraries complete.")
