"""initial — projects / style_images / comfy_workflows / cleanup_rules

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False, index=True),
        sa.Column("photo_path", sa.String(512), nullable=True),
        sa.Column("lineart_path", sa.String(512), nullable=True),
        sa.Column("mlsd_path", sa.String(512), nullable=True),
        sa.Column("depth_path", sa.String(512), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("ref_image_id", sa.String(64), nullable=True),
        sa.Column("result_path", sa.String(512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "style_images",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("qdrant_point_id", sa.String(64), nullable=True, index=True),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "comfy_workflows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workflow_key", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("json", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "cleanup_rules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("rule_type", sa.String(16), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("cleanup_rules")
    op.drop_table("comfy_workflows")
    op.drop_table("style_images")
    op.drop_table("projects")
