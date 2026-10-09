"""scripts/import_styles.py — 本地风格图目录批量入库（CLIP 向量化 + Qdrant + MySQL 元数据）

用法（在 backend/ 目录下）：
    uv run python ../scripts/import_styles.py <图片目录>
    uv run python ../scripts/import_styles.py <图片目录> --recreate   # 重建 collection

遍历目录下所有图片，逐张：CLIP 编码 → Qdrant upsert → style_images 落行。
幂等性按文件名：同名文件重复导入会跳过。
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import SessionLocal  # noqa: E402
from app.models.style_image import StyleImage  # noqa: E402
from app.services.clip_service import ClipService  # noqa: E402
from app.storage.paths import style_images_dir, to_image_ref  # noqa: E402

_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}


def main() -> None:
    parser = argparse.ArgumentParser(description="风格图批量入库")
    parser.add_argument("src_dir", help="本地风格图目录")
    parser.add_argument("--recreate", action="store_true", help="删除并重建 Qdrant collection")
    args = parser.parse_args()

    src = Path(args.src_dir)
    if not src.is_dir():
        print(f"目录不存在: {src}")
        sys.exit(1)

    clip = ClipService()
    if args.recreate:
        client = clip._get_qdrant()
        from qdrant_client import models

        from app.config import settings

        client.delete_collection(settings.QDRANT_COLLECTION)
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=models.VectorParams(
                size=settings.CLIP_VECTOR_DIM, distance=models.Distance.COSINE
            ),
        )
        print("collection 已重建")
    clip._ensure_collection()

    images = [p for p in sorted(src.iterdir()) if p.suffix.lower() in _EXTS]
    if not images:
        print("目录里没有图片")
        return

    db = SessionLocal()
    imported = skipped = 0
    try:
        existing = {row.file_path for row in db.query(StyleImage).all()}
        for img in images:
            target = style_images_dir() / img.name
            ref = f"uploads/styles/{img.name}"
            if ref in existing:
                skipped += 1
                continue
            target.write_bytes(img.read_bytes())
            style_id = f"style_{uuid.uuid4().hex}"
            vector = clip._encode_image(target)
            point_id = clip._upsert(style_id, ref, vector)
            db.add(StyleImage(id=style_id, qdrant_point_id=point_id, file_path=ref, source="import"))
            db.commit()
            imported += 1
            print(f"  [ok] {img.name}")
    finally:
        db.close()
    print(f"完成：导入 {imported}，跳过 {skipped}")


if __name__ == "__main__":
    main()
