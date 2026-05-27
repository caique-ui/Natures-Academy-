"""

Core logic for detecting Google Drive changes and deciding whether to re-index.

Three responsibilities:
  1. compute_folder_fingerprint()  — SHA-256 of folder contents
  2. fetch_drive_changes()         — uses Drive Changes API with pageToken
  3. should_reindex()              — combines fingerprint + debounce + lock checks
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, List, Optional, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def compute_folder_fingerprint(drive_service, folder_id: str) -> Tuple[str, List[Dict]]:
    """
    Fetch all files in the folder recursively and compute a SHA-256 fingerprint
    from their (id, modifiedTime, md5Checksum) tuples.

    Returns:
        (fingerprint_hex, file_list)

    The fingerprint changes if:
      - any file is added, deleted, renamed, or modified
    """
    files = _list_all_files(drive_service, folder_id)

    # Sort deterministically so order doesn't affect the hash
    key = json.dumps(
        sorted(
            [
                {
                    "id":           f["id"],
                    "modifiedTime": f.get("modifiedTime", ""),
                    "md5Checksum":  f.get("md5Checksum", ""),  # empty for Google Docs
                    "name":         f["name"],
                }
                for f in files
            ],
            key=lambda x: x["id"],
        ),
        sort_keys=True,
    ).encode()

    fingerprint = hashlib.sha256(key).hexdigest()
    return fingerprint, files


def _list_all_files(drive_service, folder_id: str) -> List[Dict]:
    """Recursively list all files under folder_id."""
    results = []
    _recurse(drive_service, folder_id, results)
    return results


def _recurse(drive_service, folder_id: str, results: list):
    page_token = None
    while True:
        kwargs = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum)",
            pageSize=1000,
        )
        if page_token:
            kwargs["pageToken"] = page_token

        resp = drive_service.files().list(**kwargs).execute()
        items = resp.get("files", [])

        for item in items:
            if item["mimeType"] == "application/vnd.google-apps.folder":
                _recurse(drive_service, item["id"], results)
            else:
                results.append(item)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


# ---------------------------------------------------------------------------
# Drive Changes API
# ---------------------------------------------------------------------------

def get_initial_page_token(drive_service) -> str:
    """
    Get a fresh startPageToken from Drive.
    Call this once when setting up a DriveSync row for the first time.
    """
    resp = drive_service.changes().getStartPageToken().execute()
    return resp.get("startPageToken", "")


def fetch_drive_changes(
    drive_service,
    page_token: str,
    folder_id: str,
) -> Tuple[str, List[str]]:
    """
    Fetch all changes since page_token was recorded.

    Returns:
        (new_page_token, list_of_changed_file_ids)

    Only returns file IDs that belong to (or were removed from) folder_id.
    The caller should store new_page_token for the next call.
    """
    changed_ids: List[str] = []
    current_token = page_token

    while True:
        resp = drive_service.changes().list(
            pageToken=current_token,
            spaces="drive",
            fields=(
                "nextPageToken, newStartPageToken, "
                "changes(fileId, removed, file(id, name, mimeType, parents, trashed))"
            ),
            includeRemoved=True,
            pageSize=1000,
        ).execute()

        for change in resp.get("changes", []):
            file_id = change.get("fileId")
            if not file_id:
                continue

            removed = change.get("removed", False)
            file    = change.get("file", {})
            trashed = file.get("trashed", False)
            parents = file.get("parents", [])
            mime    = file.get("mimeType", "")

            # Skip sub-folders themselves (their contents will appear as changes)
            if mime == "application/vnd.google-apps.folder":
                continue

            # Include if removed/trashed OR if it lives under our target folder
            if removed or trashed or _is_in_folder(drive_service, parents, folder_id):
                if file_id not in changed_ids:
                    changed_ids.append(file_id)

        # Advance token
        if "nextPageToken" in resp:
            current_token = resp["nextPageToken"]
        else:
            current_token = resp.get("newStartPageToken", current_token)
            break

    return current_token, changed_ids


def _is_in_folder(drive_service, parents: List[str], target_folder_id: str) -> bool:
    """
    Walk up the parent chain to check if target_folder_id is an ancestor.
    Cached per request via a simple set to avoid redundant API calls.
    """
    visited = set()
    queue   = list(parents)

    while queue:
        fid = queue.pop()
        if fid in visited:
            continue
        visited.add(fid)

        if fid == target_folder_id:
            return True

        try:
            meta = drive_service.files().get(
                fileId=fid, fields="parents"
            ).execute()
            queue.extend(meta.get("parents", []))
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def should_reindex(sync: "DriveSync", current_fingerprint: str) -> Tuple[bool, str]:
    """
    Given a DriveSync record and the freshly-computed fingerprint,
    return (should_index, reason_string).

    Rules (in order):
      1. Lock busy           → skip
      2. Debounce active     → skip
      3. No pending changes  → skip
      4. Fingerprint same    → skip  (belt-and-suspenders safety net)
      5. Otherwise           → index
    """
    if sync.is_indexing:
        return False, "lock_busy"

    if not sync.is_debounce_over():
        quiet_for = (timezone.now() - sync.last_change_at).total_seconds()
        remaining = sync.debounce_seconds - quiet_for
        return False, f"debounce ({remaining:.0f}s remaining)"

    if not sync.pending_file_ids:
        return False, "no_pending_changes"

    if current_fingerprint and current_fingerprint == sync.folder_fingerprint:
        return False, "fingerprint_unchanged"

    return True, "changes_detected"
