from fastapi import APIRouter, UploadFile, File, HTTPException
from storage.file_storage import save_upload

router = APIRouter()

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    file_id = save_upload(
        file_bytes,
        original_filename=file.filename,
        content_type=file.content_type,
    )

    return {"file_id": file_id}